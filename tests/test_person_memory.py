from __future__ import annotations

import shutil
from pathlib import Path

from dl_senpai.persona import build_system_prompt
from dl_senpai.person_memory import (
    ImpressionUpdate,
    PersonMemoryStore,
    PersonProfile,
    build_person_memory_brief,
    extract_traits_from_user_text,
    parse_impression_updates,
    sanitize_trait,
)

_TEST_ROOT = Path(__file__).resolve().parent / "_tmp_person_memory"


def _fresh_dir(name: str) -> Path:
    path = _TEST_ROOT / name
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_parse_impression_updates_rich() -> None:
    raw = (
        "哼，知道了\n"
        '<<<IMPRESSION {"vibe":"嘴硬心软的损友","style":"短句多、爱损人",'
        '"relation":"当熟人损友","add":["主太刀","玩怪猎"],'
        '"moment":"把冰牙龙认错还嘴硬","note":"常来问配装"}>>>'
    )
    parsed = parse_impression_updates(raw)
    assert "<<<IMPRESSION" not in parsed.text
    assert len(parsed.updates) == 1
    upd = parsed.updates[0]
    assert upd.vibe and "损友" in upd.vibe
    assert upd.style and "短句" in upd.style
    assert upd.relation and "损友" in upd.relation
    assert "主太刀" in upd.add
    assert upd.moment and "冰牙龙" in upd.moment
    assert upd.notes == "常来问配装"


def test_sanitize_trait_blocks_secrets() -> None:
    assert sanitize_trait("密码是123") is None
    assert sanitize_trait("主太刀") == "主太刀"


def test_person_memory_store_cross_group_portrait() -> None:
    tmp_path = _fresh_dir("cross")
    store = PersonMemoryStore(tmp_path, max_traits=5, max_note_chars=80)
    store.touch_name(10001, "群A昵称")
    store.apply_updates(
        10001,
        [
            ImpressionUpdate(
                add=["计算机学生", "玩怪猎"],
                remove=[],
                notes="常来问配装",
                vibe="嘴硬心软，爱互怼",
                style="短句、爱损人",
                relation="熟人损友",
                moment="认错龙还嘴硬",
            )
        ],
        display_name="群B名片",
    )
    profile = store.get(10001)
    assert profile.display_names[0] == "群B名片"
    assert "群A昵称" in profile.display_names
    assert "玩怪猎" in profile.traits
    assert "嘴硬" in profile.vibe or "互怼" in profile.vibe
    assert "短句" in profile.style
    assert "损友" in profile.relation
    assert any("认错" in m for m in profile.moments)

    again = PersonMemoryStore(tmp_path).get(10001)
    assert "互怼" in again.vibe or "嘴硬" in again.vibe
    assert again.notes == "常来问配装"


def test_person_memory_remove_and_cap() -> None:
    tmp_path = _fresh_dir("cap")
    store = PersonMemoryStore(tmp_path, max_traits=3)
    store.apply_updates(
        7,
        [ImpressionUpdate(add=["a1", "a2", "a3", "a4"], remove=[], notes=None)],
    )
    profile = store.get(7)
    assert len(profile.traits) == 3
    store.apply_updates(7, [ImpressionUpdate(add=[], remove=["a2"], notes=None)])
    profile = store.get(7)
    assert "a2" not in profile.traits


def test_build_person_memory_brief() -> None:
    empty = build_person_memory_brief(PersonProfile(user_id="1"))
    assert empty == ""
    brief = build_person_memory_brief(
        PersonProfile(
            user_id="9",
            display_names=["小明"],
            traits=["主太刀"],
            vibe="嘴硬心软的损友",
            style="短句多",
            relation="熟人损友",
            moments=["认错龙还嘴硬"],
            notes="常问配装",
        ),
        name="小明",
    )
    assert "是什么样的人" in brief
    assert "说话习惯" in brief
    assert "关系感" in brief
    assert "主太刀" in brief
    assert "认错龙" in brief


def test_extract_traits_from_user_text() -> None:
    traits = extract_traits_from_user_text("学姐我主太刀，平时爱玩怪物猎人")
    assert any("太刀" in t for t in traits)
    assert extract_traits_from_user_text("今天天气不错") == []


def test_persona_includes_person_memory() -> None:
    prompt = build_system_prompt(person_memory_enable=True)
    assert "长时印象" in prompt
    assert "是什么样的人" in prompt
    assert "<<<IMPRESSION" in prompt
    assert "长时印象" not in build_system_prompt(person_memory_enable=False)
