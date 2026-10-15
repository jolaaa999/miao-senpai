"""Phase 6A 强验收：LEGACY 模式必须仍支持全部 9 种能力（第二十/二十一节）。

**不能通过删能力达到架构收敛。** 本测试证明：
    DL_SENPAI_AGENT_MODE=legacy 时，机器人仍能
    搜索 / 写印象 / 发表情 / 发语音 / 生图 / 浏览 / 改名片 / 禁言 / 读群资源
    且这些能力全部经由**共享** ToolExecutor 实现。

2026-10-14：新增 group_read（读群公告/群文件/群相册）。
它同样注册在共享注册表里，legacy 的 marker 分发走 SharedToolDispatcher，
所以对 legacy 也是真能力——因此加进清单，而不是把它排除在验收之外。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from dl_senpai.agent.tool_dispatch import SharedToolDispatcher
from dl_senpai.agent.tools.builtin import build_production_tools, register_production_tools
from dl_senpai.agent.tools.executors import REAL_EXECUTORS
from dl_senpai.agent.tools.models import ToolResult
from dl_senpai.agent.tools.registry import ToolRegistry

CAPABILITIES = [
    "search", "impression", "sticker", "voice",
    "draw", "browse", "card", "mute", "group_read",
]


def test_all_capabilities_registered() -> None:
    tools = {t.name for t in build_production_tools()}
    assert tools == set(CAPABILITIES)


@pytest.mark.parametrize("tool", CAPABILITIES)
def test_capability_uses_shared_executor(tool: str) -> None:
    """每个能力的 executor 都是共享层那一个（不是 legacy 私有的）。"""
    tools = {t.name: t for t in build_production_tools()}
    assert tools[tool].executor is REAL_EXECUTORS[tool]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", CAPABILITIES)
async def test_legacy_dispatch_reaches_shared_executor(tool: str) -> None:
    """LEGACY 路径 dispatch 时，实际调用的是共享 executor。"""
    invoked: list[str] = []

    registry = ToolRegistry()
    register_production_tools(registry)
    for definition in registry.list():
        definition.executor = (
            lambda n: lambda ctx, call: invoked.append(n) or ToolResult(
                tool_name=n, tool_call_id=call.id, success=True,
                data={"value": n},
            )
        )(definition.name)

    from dl_senpai.agent.permissions import AllowAllPermissionService
    from dl_senpai.agent.tools.executor import ToolExecutor
    from dl_senpai.agent.tools.models import ToolCall

    executor = ToolExecutor(registry, permissions=AllowAllPermissionService())
    dispatcher = SharedToolDispatcher(executor=executor)

    await dispatcher.dispatch([ToolCall(tool_name=tool, arguments={})], None)
    assert invoked == [tool], f"{tool} 未走共享 executor"


def test_legacy_orchestration_still_present() -> None:
    """legacy orchestration 本身保留（只是不再有自己的 tool 层）。"""
    import dl_senpai.handlers as handlers

    assert hasattr(handlers, "_handle_chat")
    assert hasattr(handlers, "_handle_chat_inner")


def test_legacy_orchestration_uses_shared_dispatcher() -> None:
    """legacy orchestration 内部确实调用共享 dispatcher。

    Phase 6.5：编排主体已从 handlers.py 搬到
    `agent/legacy_orchestrator.py`，因此改为检查新位置。
    handlers 只保留 forwarding wrapper。
    """
    import inspect

    from dl_senpai.agent import legacy_orchestrator

    src = inspect.getsource(legacy_orchestrator)
    assert "_dispatch_legacy_tool_calls" in src
    # 六处工具调用都应经它转发
    assert src.count("await _dispatch_legacy_tool_calls(") >= 6, (
        "legacy orchestration 仍有未迁移的 tool dispatch"
    )


def test_handlers_is_now_a_thin_forwarder() -> None:
    """Phase 6.5：handlers 不再持有编排主体。"""
    import inspect

    import dl_senpai.handlers as handlers

    src = inspect.getsource(handlers._handle_chat_inner)
    lines = [ln for ln in src.splitlines() if ln.strip()]
    assert len(lines) < 40, (
        f"_handle_chat_inner 仍很臃肿（{len(lines)} 行），应只是 forwarding wrapper"
    )
    # 必须转发到 LegacyOrchestrator
    assert "LegacyOrchestrator" in src
    # 不得仍持有编排逻辑
    for banned in ("llm.chat(", "build_system_prompt(", "result.card_actions"):
        assert banned not in src, f"wrapper 仍含编排逻辑: {banned}"


def test_legacy_no_direct_business_calls() -> None:
    """legacy orchestration 不再直接调业务执行函数。"""
    from dl_senpai import handlers

    src = inspect.getsource(handlers)
    banned = [
        "await apply_card_actions(",
        "await apply_mute_actions(",
        "await build_image_segment(",
        "await run_browse_tasks(",
        "await build_voice_segment(",
        "maybe_pick_sticker_reply(",
    ]
    found = [b for b in banned if b in src]
    assert not found, f"legacy 仍直接调用业务执行: {found}"


def test_both_modes_share_same_executor_instance_type() -> None:
    """两个模式用的是同一个 ToolExecutor 类型（不是两份实现）。"""
    from dl_senpai.agent.entry import AgentEntry
    from dl_senpai.agent.tools.executor import ToolExecutor

    # AgentRuntime 用 ToolExecutor
    from dl_senpai.agent.runtime import AgentRuntime

    src_runtime = inspect.getsource(AgentRuntime)
    assert "ToolExecutor" in src_runtime
    # SharedToolDispatcher 也用 ToolExecutor
    src_dispatch = inspect.getsource(SharedToolDispatcher)
    assert "ToolExecutor" in src_dispatch


def test_legacy_context_adapter_exists() -> None:
    """legacy 缺上下文时有 adapter（第十一节）。"""
    from dl_senpai.agent.legacy_context import build_legacy_context

    assert callable(build_legacy_context)


def test_legacy_adapter_does_not_parse_markers() -> None:
    """LegacyChatResultAdapter 不得重新解析 marker（第五节）。"""
    from dl_senpai.agent import legacy_adapter

    src = inspect.getsource(legacy_adapter)
    banned = [
        "parse_card_actions", "parse_mute_actions", "parse_draw_request",
        "parse_browse_request", "parse_sticker_request", "parse_voice_request",
        "parse_impression_updates", "parse_search_request",
    ]
    found = [b for b in banned if b in src]
    assert not found, f"形状转换层重新解析了 marker: {found}"


def test_shared_dispatcher_does_not_implement_tool_behavior() -> None:
    """SharedToolDispatcher 只转发，不实现工具行为。"""
    from dl_senpai.agent import tool_dispatch

    src = inspect.getsource(tool_dispatch)
    for banned in ("group_card", "group_mute", "image_gen", "browser_agent"):
        # 业务模块名不应出现在 dispatch 层
        assert banned not in src, f"dispatch 层侵入了业务模块 {banned}"
