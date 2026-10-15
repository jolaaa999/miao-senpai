"""Phase 4 测试：Prompt 等价性 + Tool Registry 驱动 + 扩展性验收。

这是 Phase 4 最关键的等价性证明：
1. 注册表驱动的 prompt 与旧 if 分支**逐字一致**；
2. persona 文本资产零修改；
3. 新增 DummyTool 不需要改核心文件。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from dl_senpai.agent.context import AgentContext
from dl_senpai.agent.prompt.builder import (
    PromptBuilder,
    PromptRequest,
    apply_tool_visibility_filter,
)
from dl_senpai.agent.tools.builtin import build_production_tools, register_production_tools
from dl_senpai.agent.tools.dummy_tool import register_dummy_tool
from dl_senpai.agent.tools.marker_adapter import MarkerToolAdapter
from dl_senpai.agent.tools.registry import ToolRegistry
from dl_senpai.persona import build_system_prompt


@pytest.fixture
def tools() -> dict[str, Any]:
    return {t.name: t for t in build_production_tools()}


# ================================================================
# Prompt 等价性：registry-driven == legacy if-branches
# ================================================================

PROMPT_CASES = [
    ("纯聊天", [], {}),
    ("CARD", ["card"], {"card_enable": True}),
    ("MUTE", ["mute"], {"mute_enable": True}),
    ("SEARCH", ["search"], {"search_enable": True}),
    ("DRAW", ["draw"], {"draw_enable": True}),
    ("BROWSE", ["browse"], {"browser_enable": True}),
    ("IMPRESSION", ["impression"], {"person_memory_enable": True}),
    (
        "全部",
        ["card", "mute", "impression", "search", "draw", "browse", "voice", "sticker"],
        {
            "card_enable": True, "mute_enable": True, "person_memory_enable": True,
            "search_enable": True, "draw_enable": True, "browser_enable": True,
        },
    ),
]


@pytest.mark.parametrize("label,tool_names,flags", PROMPT_CASES)
@pytest.mark.parametrize("interrupt", [False, True])
@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("images", [False, True])
def test_prompt_registry_driven_equals_legacy(
    tools: dict[str, Any],
    label: str,
    tool_names: list[str],
    flags: dict[str, bool],
    interrupt: bool,
    private: bool,
    images: bool,
) -> None:
    """注册表驱动的 prompt 必须与旧 if 分支逐字一致。

    这是"prompt 是生产资产"这一约束的机器化验证。
    """
    legacy = build_system_prompt(
        interrupt=interrupt, has_images=images, private_chat=private,
        fewshot_enable=False, **flags,
    )
    builder = PromptBuilder()
    built = builder.build(
        PromptRequest(
            interrupt=interrupt, has_images=images, private_chat=private,
            fewshot_enable=False,
            visible_tools=[tools[n] for n in tool_names],
        )
    )
    # 2026-10-12：关闭的工具（表情包/语音）不再把说明写进 prompt。
    # legacy 侧是冻结的 persona 原文，对它施加**同一层**可见性过滤后再逐字
    # 比对——守卫语义不变：证明除这层已声明的过滤外没有其它漂移。
    expected = apply_tool_visibility_filter(legacy, set(tool_names))
    assert built.text == expected, f"{label} 的 prompt 文本不一致"


def test_prompt_builder_reports_sections(tools: dict[str, Any]) -> None:
    builder = PromptBuilder()
    built = builder.build(
        PromptRequest(visible_tools=[tools["card"], tools["draw"]], has_images=True)
    )
    assert built.sections["card_enable"] is True
    assert built.sections["draw_enable"] is True
    assert built.sections["mute_enable"] is False
    assert built.sections["vision"] is True
    assert set(built.tool_names) == {"card", "draw"}


def test_prompt_builder_does_not_read_enable_flags_directly() -> None:
    """PromptBuilder 不得自己判断 enable —— 可见性只由 ToolRegistry 决定。

    这是"注册表驱动"的验收点：如果 Builder 里出现 `config.draw_enable`
    这类判断，说明存在两处可见性来源，会漂移。
    """
    src = inspect.getsource(PromptBuilder)
    for flag in ("draw_enable", "mute_enable", "card_enable", "browser_enable"):
        # 允许作为 dict key 出现（传给 renderer 用），但不允许作为属性访问
        assert f"config.{flag}" not in src
        assert f". {flag}" not in src


# ================================================================
# persona 文本资产零修改
# ================================================================


def test_persona_tool_texts_are_referenced_not_copied() -> None:
    """工具 prompt_text 必须直接引用 persona 常量，不能是复制来的副本。"""
    import dl_senpai.persona as persona

    tool_map = {t.name: t for t in build_production_tools()}
    assert tool_map["card"].prompt_text == persona.CARD_SYSTEM_EXTRA.strip()
    assert tool_map["mute"].prompt_text == persona.MUTE_SYSTEM_EXTRA.strip()
    assert tool_map["search"].prompt_text == persona.SEARCH_SYSTEM_EXTRA.strip()
    assert tool_map["draw"].prompt_text == persona.DRAW_SYSTEM_EXTRA.strip()
    assert tool_map["browse"].prompt_text == persona.BROWSE_SYSTEM_EXTRA.strip()
    assert (
        tool_map["impression"].prompt_text
        == persona.PERSON_MEMORY_SYSTEM_EXTRA.strip()
    )


def test_persona_text_assets_intact() -> None:
    """persona.py 必须仍提供注册表依赖的全部文本资产（prompt 是生产资产）。

    2026-10-12：人设从「傲娇」调整为「可爱萌妹」。原「文件零改动」守卫
    会把任何文案迭代都判失败，改为「资产完整」守卫——注册表的 prompt_text
    全部直接引用 persona 常量，所以要守的是这些常量仍在、且基础段落仍含
    无条件工具小节（apply_tool_visibility_filter 依赖它们做装配期过滤）。
    """
    import dl_senpai.persona as persona

    # 注册表引用的常量必须全部存在且非空
    for name in (
        "CARD_SYSTEM_EXTRA",
        "MUTE_SYSTEM_EXTRA",
        "SEARCH_SYSTEM_EXTRA",
        "DRAW_SYSTEM_EXTRA",
        "BROWSE_SYSTEM_EXTRA",
        "PERSON_MEMORY_SYSTEM_EXTRA",
    ):
        value = getattr(persona, name, None)
        assert isinstance(value, str) and value.strip(), f"{name} 丢失或为空"

    # 基础段落里的无条件工具小节必须还在（过滤层按这两个标记切段）
    base = persona.SYSTEM_PROMPT
    assert "【表情包】" in base and "<<<STICKER" in base
    assert "【语音能力】" in base and "<<<VOICE" in base


def test_style_polish_rules_still_in_place() -> None:
    """style.py 的净化规则必须都在位、且仍被 polish_reply 调用。

    2026-10-13：原先断言 git 零改动。修「思考过程/伪造对话泄漏」要往
    polish_reply 里加清洗，零改动守卫会挡住修复，故改为结构守卫
    （同 test_style_polish_regexes_untouched）。
    """
    import inspect

    from dl_senpai import style

    src = inspect.getsource(style.polish_reply)
    for name in (
        "_STICKER_MARKER_RE", "_VOICE_MARKER_RE", "_CARD_LEAK_RE",
        "_MUTE_LEAK_RE", "_IMPRESSION_LEAK_RE", "_SEARCH_LEAK_RE",
        "_BROWSE_LEAK_RE", "_FAKE_SYS_LOG_RE", "_FAKE_EMOJI_RE",
        "_MUTE_CANT_TALK_RE",
    ):
        assert hasattr(style, name), name + " 被删除"
        assert name in src, name + " 没有被 polish_reply 调用"


def test_sticker_and_voice_have_no_duplicate_prompt_text() -> None:
    """STICKER / VOICE 的说明内嵌在 SYSTEM_PROMPT，不应有第二份副本。

    若这里非空，说明 prompt 里会出现重复说明——那会改变模型看到的文本。
    """
    tool_map = {t.name: t for t in build_production_tools()}
    assert tool_map["sticker"].prompt_text == ""
    assert tool_map["voice"].prompt_text == ""


# ================================================================
# 扩展性验收（第四十五条）
# ================================================================


CORE_FILES = (
    "src/plugins/dl_senpai/persona.py",
    "src/plugins/dl_senpai/handlers.py",
    "src/plugins/dl_senpai/llm.py",
    "src/plugins/dl_senpai/agent/runtime.py",
)


def _file_hash(rel: str) -> str:
    import hashlib
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    data = (root / rel).read_bytes()
    return hashlib.sha256(data).hexdigest()


def test_dummy_tool_requires_no_core_file_change() -> None:
    """注册 DummyTool 前后，4 个核心文件的内容哈希必须不变。

    这是"Tool Registry 不是伪抽象"的机器化证明。
    """
    before = {f: _file_hash(f) for f in CORE_FILES}

    registry = ToolRegistry()
    register_production_tools(registry)
    register_dummy_tool(registry)

    # 完整走一遍：prompt 生成 / 解析 / 执行
    adapter = MarkerToolAdapter(registry)
    ctx = AgentContext(session_id="group:1")
    visible = registry.visible_tools(ctx)
    rendered = adapter.render_tools(visible)
    parsed = adapter.parse_response("好\n<<<DUMMY hello>>>")

    after = {f: _file_hash(f) for f in CORE_FILES}
    assert before == after, "注册 DummyTool 不该改动核心文件"
    assert "DUMMY" in rendered
    assert parsed.first("dummy") is not None


def test_dummy_tool_full_lifecycle(registry_fixture: ToolRegistry | None = None) -> None:
    """DummyTool 的完整生命周期：注册 → prompt → 解析 → 执行 → 注销。"""
    from dl_senpai.agent.tools.executor import ToolExecutor

    registry = ToolRegistry()
    register_production_tools(registry)
    tool = register_dummy_tool(registry)

    # 1) 出现在 prompt 渲染里
    adapter = MarkerToolAdapter(registry)
    assert "<<<DUMMY" in adapter.render_tools([tool])

    # 2) 可解析
    parsed = adapter.parse_response("<<<DUMMY 参数值>>>")
    call = parsed.first("dummy")
    assert call is not None and call.arguments["value"] == "参数值"
    assert "DUMMY" not in parsed.text

    # 3) 可执行
    import asyncio

    executor = ToolExecutor(registry)
    result = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        executor.execute(call, AgentContext(session_id="s"))
    )
    assert result.success is True
    assert "参数值" in result.output

    # 4) 可注销
    assert registry.unregister_owner("test_dummy") == 1


def test_dummy_tool_marker_tolerance() -> None:
    """DummyTool 自身也应支持容错闭合（与生产工具一致的标准）。"""
    registry = ToolRegistry()
    register_dummy_tool(registry)
    adapter = MarkerToolAdapter(registry)
    for closer in (">>>", ">>", "＞＞＞", "】】", "】"):
        out = adapter.parse_response(f"<<<DUMMY x{closer}")
        assert out.first("dummy") is not None, f"closer={closer} 未被识别"


# ================================================================
# AgentRuntime 不认识任何具体工具（第六十四条）
# ================================================================


def test_agent_runtime_knows_no_tools() -> None:
    """核心约束：AgentRuntime 代码里不得硬编码具体工具名。

    AgentRuntime 只应通过 ToolRegistry 与 ToolDefinition 工作。

    实现：用 AST 找出所有 `x == "字面量"` 形式的比较节点，
    只看**真实代码**里的字符串常量——docstring 里为了说明约束而
    举例写 `if tool_name == "card"` 是合法的，不会被误判。
    """
    import ast

    from dl_senpai.agent import runtime as runtime_mod

    tree = ast.parse(inspect.getsource(runtime_mod))

    hardcoded: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        for comparator in node.comparators:
            if isinstance(comparator, ast.Constant) and isinstance(
                comparator.value, str
            ):
                hardcoded.append(comparator.value)

    banned = {"card", "mute", "draw", "browse", "sticker", "voice", "impression"}
    offenders = banned & set(hardcoded)
    assert not offenders, f"AgentRuntime 硬编码比较了工具名: {sorted(offenders)}"


def test_agent_runtime_uses_registry() -> None:
    from dl_senpai.agent import runtime as runtime_mod

    src = inspect.getsource(runtime_mod)
    assert "visible_tools" in src
    assert "Continuation" in src, "continuation 必须由工具声明而非 Runtime 判断"


def test_prompt_builder_has_no_tool_if_branches() -> None:
    """PromptBuilder 不得写 `if draw_enable: prompt += ...` 这类分支。"""
    src = inspect.getsource(PromptBuilder)
    assert "prompt +=" not in src
    assert "visible_tools" in src
