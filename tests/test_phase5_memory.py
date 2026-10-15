"""Phase 5 测试：MemoryFacade（第六十三节）。

覆盖：session history / user memory / group memory / private isolation /
cross-group isolation / legacy JSON compatibility。

**不迁移数据、不改 schema** —— 测试同时验证这一点。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dl_senpai.agent.memory_facade import (
    MemoryFacade,
    MemoryRef,
    MemoryScope,
    MemorySnapshot,
)


# ================================================================
# Scope 语义（第三十八/三十九/四十节）
# ================================================================


def test_scopes_are_explicit() -> None:
    assert {s.value for s in MemoryScope} == {
        "session", "user", "group", "global"
    }


def test_session_ref_key() -> None:
    ref = MemoryRef.for_session("group:900001")
    assert ref.scope is MemoryScope.SESSION
    assert ref.cache_key() == "session|group:900001||"


def test_user_ref_key_includes_group() -> None:
    """用户的**群语境**必须带 group_id，避免跨群串味。"""
    a = MemoryRef.for_user("10001", group_id="900001")
    b = MemoryRef.for_user("10001", group_id="900002")
    assert a.cache_key() != b.cache_key()
    assert "900001" in a.cache_key()
    assert "900002" in b.cache_key()


def test_group_ref_key() -> None:
    ref = MemoryRef.for_group("900001")
    assert ref.cache_key() == "group||900001|"


def test_private_and_group_namespaces_differ() -> None:
    """private:{uid} 与 group:{gid} 必须是不同 namespace（第四十节）。"""
    private = MemoryRef.for_session("private:10001")
    group = MemoryRef.for_session("group:900001")
    assert private.cache_key() != group.cache_key()


# ================================================================
# Snapshot
# ================================================================


def test_snapshot_defaults_empty() -> None:
    snap = MemorySnapshot()
    assert snap.history == []
    assert snap.recalled == ""
    assert snap.person_profile == ""


def test_snapshot_log_fields_only_lengths() -> None:
    """第五十五节：只记录长度，不记录内容（避免隐私泄漏）。"""
    snap = MemorySnapshot(
        history=[{"role": "user", "content": "秘密内容"}],
        recalled="敏感回忆",
        person_profile="画像",
    )
    fields = snap.as_log_fields()
    joined = " ".join(str(v) for v in fields.values())
    assert "秘密内容" not in joined
    assert "敏感回忆" not in joined
    assert fields["history_len"] == 1
    assert fields["recalled_len"] == 4


# ================================================================
# Session history（legacy JSON 兼容）
# ================================================================


def test_session_history_reads_legacy_json(tmp_path: Path) -> None:
    """历史 JSON 无需迁移即可读取。"""
    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    legacy = [
        {"role": "user", "content": "历史消息"},
        {"role": "assistant", "content": "历史回复"},
    ]
    (mem_dir / "group_900001.json").write_text(
        json.dumps(legacy, ensure_ascii=False), encoding="utf-8"
    )

    from dl_senpai.config import PluginConfig

    cfg = PluginConfig(
        DL_SENPAI_MEMORY_DIR=str(mem_dir),
        DL_SENPAI_MAX_HISTORY=0,
    )
    facade = MemoryFacade(cfg)
    history = facade.get_session_history(MemoryRef.for_session("group:900001"), cfg)
    assert history == legacy


def test_session_history_missing_returns_empty(tmp_path: Path) -> None:
    from dl_senpai.config import PluginConfig

    cfg = PluginConfig(DL_SENPAI_MEMORY_DIR=str(tmp_path / "none"))
    facade = MemoryFacade(cfg)
    assert facade.get_session_history(MemoryRef.for_session("group:x"), cfg) == []


def test_history_for_prompt_returns_recall(tmp_path: Path) -> None:
    from dl_senpai.config import PluginConfig

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    history = []
    for i in range(40):
        history.append({"role": "user", "content": f"消息{i}关于小猫"})
        history.append({"role": "assistant", "content": f"回复{i}"})
    (mem_dir / "group_900001.json").write_text(
        json.dumps(history, ensure_ascii=False), encoding="utf-8"
    )

    cfg = PluginConfig(
        DL_SENPAI_MEMORY_DIR=str(mem_dir),
        DL_SENPAI_MEMORY_RECENT_TURNS=4,
        DL_SENPAI_MEMORY_RECALL_TOP=2,
    )
    facade = MemoryFacade(cfg)
    recent, brief = facade.load_history_for_prompt(
        MemoryRef.for_session("group:900001"), "小猫", cfg
    )
    assert len(recent) <= 4
    assert isinstance(brief, str)


def test_history_recall_disabled(tmp_path: Path) -> None:
    from dl_senpai.config import PluginConfig

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    history = [{"role": "user", "content": f"m{i}"} for i in range(40)]
    (mem_dir / "group_900001.json").write_text(
        json.dumps(history, ensure_ascii=False), encoding="utf-8"
    )
    cfg = PluginConfig(DL_SENPAI_MEMORY_DIR=str(mem_dir))
    facade = MemoryFacade(cfg)
    _, brief = facade.load_history_for_prompt(
        MemoryRef.for_session("group:900001"), "x", cfg, enable_recall=False
    )
    assert brief == ""


# ================================================================
# User memory（跨群共享偏好，群语境隔离）
# ================================================================


def test_person_profile_reads_legacy_json(tmp_path: Path) -> None:
    from dl_senpai.config import PluginConfig

    pm_dir = tmp_path / "person_memory"
    pm_dir.mkdir()
    profile = {
        "display_names": ["小明"],
        "traits": ["爱猫"],
        "notes": "养了两只猫",
    }
    (pm_dir / "user_10001.json").write_text(
        json.dumps(profile, ensure_ascii=False), encoding="utf-8"
    )

    cfg = PluginConfig(DL_SENPAI_PERSON_MEMORY_DIR=str(pm_dir))
    facade = MemoryFacade(cfg)
    ref = MemoryRef.for_user("10001", group_id="900001")
    brief, prof = facade.get_person_profile(ref, "小明", "猫", cfg)
    assert isinstance(brief, str)
    assert prof.user_id == "10001"


def test_person_profile_shared_across_groups(tmp_path: Path) -> None:
    """第三十九节：**个人长期偏好允许跨群共享**。"""
    import dl_senpai.person_memory as pm_mod
    from dl_senpai.config import PluginConfig
    from dl_senpai.person_memory import PersonMemoryStore

    pm_dir = tmp_path / "person_memory"
    pm_dir.mkdir()
    store = PersonMemoryStore(pm_dir)
    facade = MemoryFacade(PluginConfig(DL_SENPAI_PERSON_MEMORY_DIR=str(pm_dir)))

    a = MemoryRef.for_user("10001", group_id="900001")
    b = MemoryRef.for_user("10001", group_id="900002")

    # 用户画像按 QQ 存，与 group 无关 —— 两个群读到同一份
    store.touch_name("10001", "小明")
    _, prof_a = facade.get_person_profile(a, "小明", "", None)
    _, prof_b = facade.get_person_profile(b, "小明", "", None)
    assert prof_a.user_id == prof_b.user_id == "10001"
    assert prof_a.display_names == prof_b.display_names


def test_group_style_isolated_between_groups(tmp_path: Path) -> None:
    """第三十九节：**群上下文禁止共享**。"""
    from dl_senpai.config import PluginConfig
    from dl_senpai.group_style import GroupStyleStore

    gs_dir = tmp_path / "group_style"
    gs_dir.mkdir()
    store = GroupStyleStore(gs_dir)
    store.observe("900001", "这个群喜欢说喵喵喵")
    store.observe("900001", "喵喵喵")
    store.observe("900001", "喵喵喵")
    store.observe("900002", "完全不同的说法")

    cfg = PluginConfig(
        DL_SENPAI_GROUP_STYLE_DIR=str(gs_dir),
        DL_SENPAI_GROUP_STYLE_MIN_COUNT=1,
        DL_SENPAI_GROUP_STYLE_TOP_N=5,
    )
    facade = MemoryFacade(cfg)
    brief_a = facade.get_group_style("900001", cfg)
    brief_b = facade.get_group_style("900002", cfg)
    # 两个群的风格互不污染
    assert "喵喵喵" in brief_a
    assert "喵喵喵" not in brief_b


# ================================================================
# Affection
# ================================================================


def test_affection_reads_legacy_json(tmp_path: Path) -> None:
    from dl_senpai.affection import AffectionStore, UserAffection
    from dl_senpai.config import PluginConfig

    aff_dir = tmp_path / "affection"
    aff_dir.mkdir()
    AffectionStore(aff_dir).save_group(
        "900001", {"10001": UserAffection(user_id="10001", value=42)}
    )

    cfg = PluginConfig(DL_SENPAI_AFFECTION_DIR=str(aff_dir))
    facade = MemoryFacade(cfg)
    brief, value = facade.get_affection("900001", "10001", cfg)
    assert value == 42
    assert isinstance(brief, str)


def test_affection_isolated_between_groups(tmp_path: Path) -> None:
    """同一用户在不同群的好感度互相独立。"""
    from dl_senpai.affection import AffectionStore, UserAffection
    from dl_senpai.config import PluginConfig

    aff_dir = tmp_path / "affection"
    aff_dir.mkdir()
    store = AffectionStore(aff_dir)
    store.save_group("900001", {"10001": UserAffection(user_id="10001", value=10)})
    store.save_group("900002", {"10001": UserAffection(user_id="10001", value=99)})

    cfg = PluginConfig(DL_SENPAI_AFFECTION_DIR=str(aff_dir))
    facade = MemoryFacade(cfg)
    _, v1 = facade.get_affection("900001", "10001", cfg)
    _, v2 = facade.get_affection("900002", "10001", cfg)
    assert v1 == 10
    assert v2 == 99


def test_record_interaction_writes_legacy_format(tmp_path: Path) -> None:
    """写入必须保持旧 JSON 结构（不改 schema）。"""
    from dl_senpai.config import PluginConfig

    aff_dir = tmp_path / "affection"
    cfg = PluginConfig(DL_SENPAI_AFFECTION_DIR=str(aff_dir))
    facade = MemoryFacade(cfg)
    facade.record_interaction("900001", "10001", 5, "小明", cfg)

    raw = json.loads((aff_dir / "group_900001.json").read_text(encoding="utf-8"))
    assert "users" in raw
    assert "updated_at" in raw
    assert "10001" in raw["users"]


# ================================================================
# Inner state
# ================================================================


def test_inner_state_reads(tmp_path: Path) -> None:
    from dl_senpai.config import PluginConfig
    from dl_senpai.inner_state import InnerStateStore

    is_dir = tmp_path / "inner_state"
    is_dir.mkdir()
    InnerStateStore(is_dir).apply_after_reply(
        "global", reply_len=10, mentioned=True, interrupt=False, private_chat=False
    )

    cfg = PluginConfig(DL_SENPAI_INNER_STATE_DIR=str(is_dir))
    facade = MemoryFacade(cfg)
    brief = facade.get_inner_state("global", cfg)
    assert isinstance(brief, str)


# ================================================================
# Snapshot 聚合
# ================================================================


def test_build_snapshot_minimal(tmp_path: Path) -> None:
    from dl_senpai.config import PluginConfig

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    cfg = PluginConfig(DL_SENPAI_MEMORY_DIR=str(mem_dir))
    facade = MemoryFacade(cfg)

    snap = facade.build_snapshot(
        MemoryRef.for_session("group:900001"),
        query="你好",
        name="小明",
        config=cfg,
        want={},
    )
    assert isinstance(snap, MemorySnapshot)
    assert snap.history == []


def test_build_snapshot_with_person_memory(tmp_path: Path) -> None:
    from dl_senpai.config import PluginConfig

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    pm_dir = tmp_path / "person_memory"
    pm_dir.mkdir()

    cfg = PluginConfig(
        DL_SENPAI_MEMORY_DIR=str(mem_dir),
        DL_SENPAI_PERSON_MEMORY_DIR=str(pm_dir),
    )
    facade = MemoryFacade(cfg)
    snap = facade.build_snapshot(
        MemoryRef.for_user("10001", group_id="900001"),
        query="你好",
        name="小明",
        config=cfg,
        want={"person_memory": True},
    )
    assert "person_profile" in snap.data


# ================================================================
# 不迁移数据的证明
# ================================================================


def test_facade_does_not_create_new_storage() -> None:
    """Facade 不得引入新的存储文件/目录名。"""
    import inspect

    from dl_senpai.agent import memory_facade

    src = inspect.getsource(memory_facade)
    # 不应出现向量库/新数据库相关关键字
    for banned in ("vector", "embedding", "chroma", "faiss", "sqlite", "database"):
        assert banned not in src.lower(), f"Facade 引入了 {banned}"
