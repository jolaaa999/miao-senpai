"""Phase 6A：Legacy 与 Runtime 共享 Tool Runtime 的验证。

覆盖第二十三/二十四/二十五节：
    Legacy marker behavior snapshot
    Legacy tool behavior snapshot
    No Double Execution

以及第十八/十九/二十节的强验收：
    Marker Parser implementation count = 1
    Tool production executor count = 8
    LEGACY mode 仍支持 8 种能力
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dl_senpai.agent.legacy_adapter import LegacyChatResultAdapter
from dl_senpai.agent.tool_dispatch import (
    DispatchOutcome,
    SharedToolDispatcher,
    tool_call_for_browse,
    tool_call_for_draw,
    tool_call_for_search,
    tool_call_for_sticker,
    tool_call_for_voice,
    tool_calls_from_card_actions,
    tool_calls_from_impressions,
    tool_calls_from_mute_actions,
)
from dl_senpai.agent.tools.models import ToolCall, ToolCallSource, ToolResult

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"

PARSERS = {
    "parse_card_actions",
    "parse_mute_actions",
    "parse_draw_request",
    "parse_browse_request",
    "parse_sticker_request",
    "parse_voice_request",
    "parse_impression_updates",
    "parse_search_request",
}

EXECUTOR_NAMES = {
    "card": "execute_card",
    "mute": "execute_mute",
    "draw": "execute_draw",
    "browse": "execute_browse",
    "sticker": "execute_sticker",
    "voice": "execute_voice",
    "impression": "execute_impression",
    "search": "execute_search",
}


# ================================================================
# 静态验收：parser 单一来源
# ================================================================


def test_parser_implementation_count_is_one() -> None:
    """解析实现只有 MarkerToolAdapter 一套。

    判定：除 ToolDefinition（builtin.py）与形状转换层（legacy_adapter.py）外，
    没有别的模块 import 8 个 parse_* 函数。
    """
    allowed = {
        "agent/tools/builtin.py",       # ToolDefinition.parser 定义
        "agent/legacy_adapter.py",       # 形状转换（不解析）
        "agent/tools/marker_adapter.py",
    }
    # 定义 parser 的模块自身（含模块内互相调用）
    definitions = {
        "group_card.py", "group_mute.py", "image_gen.py", "browser_agent.py",
        "stickers.py", "voice.py", "person_memory.py", "web_search.py",
        "reply_quote.py",   # 复用 user_intent_text
    }

    offenders: list[str] = []
    for path in PKG.rglob("*.py"):
        rel = str(path.relative_to(PKG)).replace("\\", "/")
        if rel in allowed or path.name in definitions:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in PARSERS:
                        offenders.append(f"{rel}:{node.lineno} {alias.name}")
            # 也要抓"未 import 但通过模块属性调用"的情况
            if isinstance(node, ast.Name) and node.id in PARSERS:
                offenders.append(f"{rel}:{node.lineno} {node.id}(call)")
    assert not offenders, "parser 出现了第二套调用者:\n" + "\n".join(offenders)


def test_llm_finalize_delegates_to_shared_adapter() -> None:
    """`_finalize_result` 必须委托共享 adapter，不再自己链式 parse。"""
    src = (PKG / "llm.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_finalize_result":
            target = node
            break
    assert target is not None

    names = {
        n.id for n in ast.walk(target) if isinstance(n, ast.Name)
    }
    # 不得直接调用 8 个 parser
    assert not (names & PARSERS), f"_finalize_result 仍直接调用 parser: {names & PARSERS}"
    # 必须委托
    assert "MarkerToolAdapter" in src


# ================================================================
# 静态验收：executor 单一来源
# ================================================================


@pytest.mark.parametrize("tool,fn", list(EXECUTOR_NAMES.items()))
def test_each_tool_has_exactly_one_executor(tool: str, fn: str) -> None:
    """8 个工具各只有 1 个 production executor 定义。"""
    sites: list[str] = []
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == fn:
                    sites.append(str(path.relative_to(PKG)))
    assert len(sites) == 1, f"{tool} 的 executor 定义了 {len(sites)} 处: {sites}"


def test_handlers_does_not_call_legacy_executors_directly() -> None:
    """handlers 不得直接调用业务执行函数（必须经 ToolExecutor）。"""
    banned = {
        "apply_card_actions",
        "apply_mute_actions",
        "build_image_segment",
        "run_browse_tasks",
        "build_voice_segment",
        "maybe_pick_sticker_reply",
    }
    tree = ast.parse((PKG / "handlers.py").read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in banned:
            found.append(f"{node.id}@{node.lineno}")
    assert not found, f"handlers 仍直接调用 legacy executor: {found}"


def test_no_legacy_tool_executor_created() -> None:
    """禁止创建 LegacyToolExecutor（第十节）。"""
    for path in PKG.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                assert "LegacyToolExecutor" not in node.name, (
                    f"{path.name} 创建了 LegacyToolExecutor"
                )


# ================================================================
# 旧形状 → ToolCall 转换
# ================================================================


def test_card_actions_to_tool_calls() -> None:
    action = MagicMock(user_id=123, card="小鱼干", kind="request")
    calls = tool_calls_from_card_actions([action])
    assert len(calls) == 1
    assert calls[0].tool_name == "card"
    assert calls[0].arguments["user_id"] == 123
    assert calls[0].arguments["card"] == "小鱼干"
    assert calls[0].source is ToolCallSource.INTERNAL


def test_mute_actions_to_tool_calls() -> None:
    action = MagicMock(user_id=456, duration=60, kind="request")
    calls = tool_calls_from_mute_actions([action])
    assert calls[0].tool_name == "mute"
    assert calls[0].arguments["duration"] == 60


def test_impressions_to_tool_calls() -> None:
    upd = MagicMock(add=["爱猫"], remove=[], notes=None)
    calls = tool_calls_from_impressions([upd])
    assert calls[0].tool_name == "impression"
    assert "爱猫" in calls[0].arguments["add"]


def test_optional_tool_calls_respect_force() -> None:
    assert tool_call_for_sticker("傲娇", force=False) is None
    assert tool_call_for_voice(force=False, override="x") is None
    assert tool_call_for_draw("cat", force=False) is None
    assert tool_call_for_browse([], force=False) is None
    # search 由 llm 循环执行，不需要二次 dispatch
    assert tool_call_for_search(searched=True) is None

    assert tool_call_for_sticker("傲娇", force=True) is not None
    assert tool_call_for_voice(force=True, override="x") is not None
    assert tool_call_for_draw("cat", force=True) is not None


# ================================================================
# Marker snapshot：ToolCall → ChatResult 形状
# ================================================================


def test_legacy_adapter_converts_card() -> None:
    adapter = LegacyChatResultAdapter()
    result = adapter.convert(
        [ToolCall(tool_name="card", arguments={"user_id": 1, "card": "x", "kind": "request"})],
        text="好",
    )
    assert len(result.card_actions) == 1
    assert result.card_actions[0].user_id == 1
    assert result.text == "好"


def test_legacy_adapter_converts_all_eight() -> None:
    """8 种工具都能转换（形状完整）。"""
    from dl_senpai.agent.tools.models import ToolCall as TC

    adapter = LegacyChatResultAdapter()
    calls = [
        TC(tool_name="card", arguments={"user_id": 1, "card": "x", "kind": "request"}),
        TC(tool_name="mute", arguments={"user_id": 2, "duration": 60, "kind": "request"}),
        TC(tool_name="impression", arguments={"add": ["爱猫"], "remove": [], "notes": None}),
        TC(tool_name="sticker", arguments={"query": "傲娇"}, raw="<<<STICKER 傲娇>>>"),
        TC(tool_name="voice", arguments={"override": "晚安"}),
        TC(tool_name="draw", arguments={"prompt": "cat", "force": True}),
        TC(tool_name="browse", arguments={"tasks": [{"kind": "pixiv"}]}),
        TC(tool_name="search", arguments={"queries": ["天气"]}),
    ]
    result = adapter.convert(calls, text="正文")
    assert len(result.card_actions) == 1
    assert len(result.mute_actions) == 1
    assert len(result.impression_updates) == 1
    assert result.sticker_query == "傲娇"
    assert result.sticker_force is True
    assert result.voice_override == "晚安"
    assert result.voice_force is True
    assert result.draw_prompt == "cat"
    assert result.draw_force is True
    assert len(result.browse_tasks) == 1
    assert result.browse_force is True
    assert result.searched is True


def test_legacy_adapter_preserves_marker_order() -> None:
    """多个同类型 marker 的**顺序**必须保持。"""
    adapter = LegacyChatResultAdapter()
    calls = [
        ToolCall(tool_name="card", arguments={"user_id": 1, "card": "a"}, position=0),
        ToolCall(tool_name="card", arguments={"user_id": 2, "card": "b"}, position=1),
    ]
    result = adapter.convert(calls, text="")
    assert [a.user_id for a in result.card_actions] == [1, 2]


def test_legacy_adapter_handles_bad_duration() -> None:
    """非法 duration 不应让 adapter 崩溃。"""
    adapter = LegacyChatResultAdapter()
    result = adapter.convert(
        [ToolCall(tool_name="mute", arguments={"user_id": 1, "duration": "很久"})],
        text="",
    )
    assert result.mute_actions[0].duration == 60  # 回退默认值


def test_legacy_adapter_empty_calls() -> None:
    adapter = LegacyChatResultAdapter()
    result = adapter.convert([], text="纯文本")
    assert result.text == "纯文本"
    assert result.card_actions == []
    assert result.searched is False


# ================================================================
# No Double Execution（第二十五节）
# ================================================================


@pytest.mark.asyncio
async def test_dispatcher_executes_each_call_once() -> None:
    """同一 ToolCall 不会被执行两次。"""
    executed: list[str] = []

    class FakeExecutor:
        async def execute(self, call, context=None, *, dry_run=False):
            executed.append(call.id)
            return ToolResult(tool_name=call.tool_name, tool_call_id=call.id, success=True)

        def set_journal(self, journal):
            pass

    dispatcher = SharedToolDispatcher(executor=FakeExecutor())
    calls = [
        ToolCall(tool_name="card", arguments={"user_id": 1, "card": "x"}),
        ToolCall(tool_name="voice", arguments={"override": "hi"}),
    ]
    outcome = await dispatcher.dispatch(calls, None)
    assert len(executed) == 2
    assert len(outcome.results) == 2


@pytest.mark.asyncio
async def test_dispatcher_skips_already_executed() -> None:
    """journal 里已记录的 tool_call_id 必须跳过（幂等）。"""
    from dl_senpai.agent.journal import ExecutionJournal

    executed: list[str] = []

    class FakeExecutor:
        async def execute(self, call, context=None, *, dry_run=False):
            executed.append(call.id)
            return ToolResult(tool_name=call.tool_name, tool_call_id=call.id, success=True)

        def set_journal(self, journal):
            pass

    journal = ExecutionJournal()
    dispatcher = SharedToolDispatcher(executor=FakeExecutor(), journal=journal)

    call = ToolCall(tool_name="mute", arguments={"user_id": 1, "duration": 60})
    journal.start(call.id, "mute")  # 模拟已执行

    await dispatcher.dispatch([call], None)
    assert executed == [], "已执行的 call 又被执行了一次"


@pytest.mark.asyncio
async def test_no_double_execution_with_real_executor() -> None:
    """端到端：legacy 路径下同一调用只触发一次业务副作用。"""
    from dl_senpai.agent.journal import ExecutionJournal
    from dl_senpai.agent.tools.registry import ToolRegistry

    call_count: list[str] = []

    registry = ToolRegistry()
    from dl_senpai.agent.tools.builtin import build_production_tools

    for tool in build_production_tools():
        registry.register(tool)
    # 替换成计数 executor
    for tool in registry.list():
        tool.executor = (
            lambda n: lambda c, k: call_count.append(n) or ToolResult(
                tool_name=n, tool_call_id=c.id, success=True,
                data={"value": n},
            )
        )(tool.name)

    from dl_senpai.agent.tools.executor import ToolExecutor
    from dl_senpai.agent.permissions import AllowAllPermissionService

    journal = ExecutionJournal()
    executor = ToolExecutor(registry, permissions=AllowAllPermissionService(), journal=journal)
    dispatcher = SharedToolDispatcher(executor=executor, journal=journal)

    calls = tool_calls_from_card_actions([MagicMock(user_id=1, card="x", kind="request")])
    await dispatcher.dispatch(calls, None)
    await dispatcher.dispatch(calls, None)  # 第二次应被跳过

    assert call_count == ["card"], f"card 被执行了 {len(call_count)} 次"


# ================================================================
# Legacy capability matrix（第二十/二十一节）
# ================================================================


LEGACY_CAPABILITIES = [
    "search", "impression", "sticker", "voice",
    "draw", "browse", "card", "mute",
]


@pytest.mark.parametrize("tool", LEGACY_CAPABILITIES)
def test_legacy_capability_has_shared_executor(tool: str) -> None:
    """LEGACY 的 8 种能力全部由共享 executor 提供。"""
    from dl_senpai.agent.tools.builtin import build_production_tools
    from dl_senpai.agent.tools.executors import REAL_EXECUTORS

    tools = {t.name: t for t in build_production_tools()}
    assert tool in tools
    assert tools[tool].executor is REAL_EXECUTORS[tool], (
        f"{tool} 的 legacy 路径没有使用共享 executor"
    )


@pytest.mark.parametrize("tool", LEGACY_CAPABILITIES)
def test_legacy_capability_has_converter(tool: str) -> None:
    """每个能力都有"旧形状 → ToolCall"的转换器（保证 legacy 可用）。"""
    import dl_senpai.agent.tool_dispatch as td

    names = dir(td)
    # card/mute/impression 用批量转换；其余用单个构造器
    expected = {
        "card": "tool_calls_from_card_actions",
        "mute": "tool_calls_from_mute_actions",
        "impression": "tool_calls_from_impressions",
        "sticker": "tool_call_for_sticker",
        "voice": "tool_call_for_voice",
        "draw": "tool_call_for_draw",
        "browse": "tool_call_for_browse",
        "search": "tool_call_for_search",
    }
    assert expected[tool] in names, f"{tool} 缺少转换器 {expected[tool]}"


def test_shared_dispatcher_is_singleton_in_handlers() -> None:
    """handlers 使用单例 dispatcher（避免每次新建 executor）。"""
    src = (PKG / "handlers.py").read_text(encoding="utf-8")
    assert "_get_shared_dispatcher" in src
    assert "SharedToolDispatcher()" in src


# ================================================================
# DispatchOutcome
# ================================================================


def test_dispatch_outcome_grouping() -> None:
    outcome = DispatchOutcome()
    outcome.add(ToolResult(tool_name="card", success=True, data={"value": "a"}))
    outcome.add(ToolResult(tool_name="card", success=True, data={"value": "b"}))
    outcome.add(ToolResult(tool_name="mute", success=False))
    assert outcome.any_success is True
    assert len(outcome.successful("card")) == 2
    assert outcome.successful("mute") == []
    assert outcome.first_value("card") == "a"


def test_dispatch_outcome_empty() -> None:
    outcome = DispatchOutcome()
    assert outcome.any_success is False
    assert outcome.first_value("card") is None
