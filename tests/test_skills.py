"""技能层（Skills）：一份说明书怎么被发现、被命中、被注入。

这层最容易长歪的两个方向，测试就是用来钉住它们的：
    1. 变成"常驻提示词"：没 triggers 的技能若也能命中，等于每条消息都在注入；
    2. 变成"第二个工具层"：技能只该产出文本，不该有执行入口。

所以这里既测正向（命中就注入、注入有预算），也测反向（不命中一个字符都不加、
目录不存在不抛异常、解析失败的单个技能不连坐）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dl_senpai.config import PluginConfig
from dl_senpai.llm import SenpaiLLM
from dl_senpai.observability.retry import NoWaitRetryPolicy
from dl_senpai.skills import (
    SkillRegistry,
    build_skill_brief,
    get_skill_registry,
    reset_skill_registry,
)


def write_skill(root: Path, name: str, body: str, *, triggers: str, description: str = "说明") -> None:
    """在 root 下造一份技能文档。"""
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\ntriggers: {triggers}\n---\n{body}",
        encoding="utf-8",
    )


def make_config(tmp_path: Path, **overrides: Any) -> PluginConfig:
    base: dict[str, Any] = dict(
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="test-model",
        DL_SENPAI_SEARCH_ENABLE=False,
        DL_SENPAI_DRAW_ENABLE=False,
        DL_SENPAI_BROWSER_ENABLE=False,
        DL_SENPAI_PERSON_MEMORY_ENABLE=False,
        DL_SENPAI_FEWSHOT_ENABLE=False,
        DL_SENPAI_LINK_READ_ENABLE=False,
        DL_SENPAI_KNOWLEDGE_ENABLE=False,
        DL_SENPAI_KNOWLEDGE_AUTO=False,
        DL_SENPAI_SKILL_ENABLE=True,
        DL_SENPAI_SKILL_DIR=str(tmp_path),
    )
    base.update(overrides)
    return PluginConfig(**base)


@pytest.fixture(autouse=True)
def _fresh_registry() -> Any:
    """每个用例都从干净缓存开始：registry 是进程级缓存，不清会串味。"""
    reset_skill_registry()
    yield
    reset_skill_registry()


# ================================================================
# 1. 解析与匹配
# ================================================================


def test_parses_frontmatter_into_spec(tmp_path: Path) -> None:
    write_skill(tmp_path, "ppt-design", "正文", triggers="ppt, 幻灯片", description="做 PPT 规范")
    specs = SkillRegistry(root=tmp_path).load()
    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "ppt-design"
    assert spec.description == "做 PPT 规范"
    assert spec.triggers == ("ppt", "幻灯片")


def test_match_is_case_insensitive_and_counts_hits(tmp_path: Path) -> None:
    write_skill(tmp_path, "a", "A", triggers="ppt")
    write_skill(tmp_path, "b", "B", triggers="ppt, 幻灯片")
    reg = SkillRegistry(root=tmp_path)
    # 两个关键词都命中的排前面：命中数就是"有多确定要它"
    hits = reg.match("帮我做个 PPT 幻灯片", limit=2)
    assert [s.name for s in hits] == ["b", "a"]
    assert reg.match("今天天气不错") == []


def test_skill_without_triggers_never_matches(tmp_path: Path) -> None:
    """没 triggers 的技能必须永不自动注入 —— 否则等于常驻提示词。"""
    d = tmp_path / "always"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: always\ndescription: x\ntriggers:\n---\n正文", encoding="utf-8")
    reg = SkillRegistry(root=tmp_path)
    assert reg.match("随便什么话") == []


def test_match_respects_limit(tmp_path: Path) -> None:
    for i in range(3):
        write_skill(tmp_path, f"s{i}", "正文", triggers="ppt")
    reg = SkillRegistry(root=tmp_path)
    assert len(reg.match("做个 ppt", limit=1)) == 1
    assert len(reg.match("做个 ppt", limit=2)) == 2


def test_missing_dir_is_tolerated(tmp_path: Path) -> None:
    """目录不存在 = 没有技能，不是错误（默认关闭时就是这种状态）。"""
    reg = SkillRegistry(root=tmp_path / "nope")
    assert reg.load() == []
    assert reg.match("做个 ppt") == []


def test_broken_skill_does_not_break_others(tmp_path: Path) -> None:
    """没有正文的技能被跳过，其他技能照常可用（不连坐）。"""
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\nname: bad\ntriggers: ppt\n---\n", encoding="utf-8")
    write_skill(tmp_path, "good", "正文", triggers="ppt")
    specs = SkillRegistry(root=tmp_path).load()
    assert [s.name for s in specs] == ["good"]


# ================================================================
# 2. 注入与预算
# ================================================================


def test_brief_is_empty_when_nothing_matches(tmp_path: Path) -> None:
    write_skill(tmp_path, "ppt-design", "正文", triggers="ppt")
    cfg = make_config(tmp_path)
    assert build_skill_brief("今天吃什么", cfg) == ""


def test_brief_contains_body_and_is_clipped(tmp_path: Path) -> None:
    write_skill(tmp_path, "ppt-design", "很长" * 500, triggers="ppt")
    cfg = make_config(tmp_path, DL_SENPAI_SKILL_MAX_CHARS=100)
    brief = build_skill_brief("帮我做 ppt", cfg)
    assert "【技能：ppt-design】" in brief
    assert "已截断" in brief
    assert len(brief) < 400  # 预算生效，没有把 1000 字正文整段塞进去


def test_registry_is_cached_by_dir(tmp_path: Path) -> None:
    """同一目录只扫一次盘：这是"装了 20 个技能不影响闲聊"的前提。"""
    write_skill(tmp_path, "a", "A", triggers="ppt")
    assert get_skill_registry(tmp_path) is get_skill_registry(tmp_path)


# ================================================================
# 3. 端到端：真的进了这一轮的提示
# ================================================================


async def _run_chat(cfg: PluginConfig, user_text: str) -> dict[str, Any]:
    llm = SenpaiLLM(cfg, retry=NoWaitRetryPolicy())
    calls: list[dict[str, Any]] = []

    async def fake_create(**kwargs: Any) -> MagicMock:
        calls.append(kwargs)
        msg = MagicMock()
        msg.content = "好呀"
        choice = MagicMock()
        choice.message = msg
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage = None
        return resp

    client = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=fake_create)
    llm.use_client(client)
    await llm.chat([], user_text, sender_name="小明")
    assert len(calls) == 1
    return calls[0]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_chat_injects_skill_when_enabled(tmp_path: Path) -> None:
    write_skill(tmp_path, "ppt-design", "先定大纲再排版。", triggers="ppt, 幻灯片")
    payload = await _run_chat(make_config(tmp_path), "帮我做个 PPT")
    assert "【技能：ppt-design】" in payload
    assert "先定大纲再排版。" in payload


@pytest.mark.asyncio
async def test_chat_adds_nothing_when_disabled(tmp_path: Path) -> None:
    """开关关着时，即使技能命中也不能出现在提示里。"""
    write_skill(tmp_path, "ppt-design", "先定大纲再排版。", triggers="ppt")
    cfg = make_config(tmp_path, DL_SENPAI_SKILL_ENABLE=False)
    payload = await _run_chat(cfg, "帮我做个 PPT")
    assert "【技能：" not in payload


@pytest.mark.asyncio
async def test_chat_adds_nothing_when_no_skill_matches(tmp_path: Path) -> None:
    write_skill(tmp_path, "ppt-design", "先定大纲再排版。", triggers="ppt")
    payload = await _run_chat(make_config(tmp_path), "今天有点累")
    assert "【技能：" not in payload


def test_pilot_skill_ships_and_loads() -> None:
    """仓库里那份试点技能必须能被真实加载（否则等于没交付）。"""
    from pathlib import Path as _P

    root = _P(__file__).resolve().parents[1] / "data" / "skills"
    specs = SkillRegistry(root=root).load()
    names = {s.name for s in specs}
    assert "ppt-design" in names
    spec = next(s for s in specs if s.name == "ppt-design")
    assert spec.triggers, "试点技能必须有触发词，否则永远不会被注入"
    assert "大纲" in spec.path.read_text(encoding="utf-8")


# ================================================================
# 4. 热重载与清单
# ================================================================


def test_hot_reload_picks_up_new_skill(tmp_path: Path) -> None:
    """新增一个技能后，不重置 registry 也应该能命中。

    真实场景：调技能文档不该停机。这条测试是"改完就能问"的机器保证。
    """
    write_skill(tmp_path, "a", "A 的正文", triggers="ppt")
    cfg = make_config(tmp_path)
    assert build_skill_brief("做个 ppt", cfg) != ""

    write_skill(tmp_path, "b", "B 的正文", triggers="周报")
    reg = get_skill_registry(tmp_path)
    assert reg.maybe_reload() is True
    assert any(s.name == "b" for s in reg.all())
    assert "B 的正文" in build_skill_brief("写个周报", cfg)


def test_hot_reload_is_noop_when_unchanged(tmp_path: Path) -> None:
    """文件没变就不要重扫 —— 否则每条消息都在解析一遍目录。"""
    write_skill(tmp_path, "a", "A", triggers="ppt")
    reg = get_skill_registry(tmp_path)
    reg.maybe_reload()
    assert reg.maybe_reload() is False


def test_catalog_text_lists_skills_and_triggers(tmp_path: Path) -> None:
    """清单是给人看的：名字、说明、触发词三者都要在。"""
    from dl_senpai.skills import catalog_text

    write_skill(tmp_path, "ppt-design", "正文", triggers="ppt, 幻灯片", description="做 PPT 规范")
    d = tmp_path / "silent"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: silent\ndescription: 无触发\n---\n正文", encoding="utf-8")
    text = catalog_text(SkillRegistry(root=tmp_path))
    assert "ppt-design：做 PPT 规范 [触发: ppt/幻灯片]" in text
    assert "不会自动注入" in text  # 没触发词的技能要在清单里显式提醒
