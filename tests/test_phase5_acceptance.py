"""Phase 5 核心验收：删掉新架构后，生产 Agent 是否无法运行。

这是 Phase 5「真正完成切流」的判定标准（第七十六节）。

做法不是真的删文件，而是**模拟"新架构不可用"**：
    - AgentRuntime 缺失 → AgentEntry 应走 fallback（说明它现在依赖 runtime）
    - 生产路径确实经过 AgentEntry（而不是绕过它直接调 _handle_chat）

如果生产路径根本不经过 AgentEntry/AgentRuntime，
那么"拿掉它们生产照样跑"——Phase 5 就没有真正切流。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest


# ================================================================
# 1. 生产路径确实经过 AgentEntry
# ================================================================


def test_legacy_bridge_routes_through_agent_entry() -> None:
    """Router → legacy_bridge → AgentEntry 是唯一路径。"""
    from dl_senpai import legacy_bridge

    src = inspect.getsource(legacy_bridge)
    assert "AgentEntry" in src or "_get_agent_entry" in src, (
        "legacy_bridge 没有经过 AgentEntry"
    )
    assert "entry.handle" in src, "legacy_bridge 没有调用 AgentEntry.handle"


def test_legacy_bridge_no_longer_calls_handler_directly_in_path() -> None:
    """`_handle_chat` 只能作为 legacy_handler 注入，不能直接作为主路径。

    检查：`_handle_chat` 的出现位置应在 `_legacy_handler_factory` 内部，
    而不是在 `handle_agent_message` 的执行体里。
    """
    import ast

    from dl_senpai import legacy_bridge

    src = inspect.getsource(legacy_bridge)
    tree = ast.parse(src)

    factory_lines: set[int] = set()
    main_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            rng = range(node.lineno, (node.end_lineno or node.lineno) + 1)
            if node.name == "_legacy_handler_factory":
                factory_lines |= set(rng)
            elif node.name == "handle_agent_message":
                main_lines |= set(rng)

    for i, line in enumerate(src.splitlines(), start=1):
        if "_handle_chat" in line:
            # 允许出现在 import 或 factory 内；不允许出现在 handle_agent_message 内
            assert i not in main_lines, (
                "handle_agent_message 直接调用了 _handle_chat（应经 AgentEntry）"
            )


def test_agent_entry_is_reachable_from_runtime() -> None:
    """Runtime 能提供 AgentEntry（懒构建）。"""
    from dl_senpai.core.runtime import Runtime

    rt = Runtime().build()
    assert hasattr(rt, "agent_entry")


# ================================================================
# 2. 拿掉 AgentRuntime → 生产行为改变（证明它被真正使用）
# ================================================================


@pytest.mark.asyncio
async def test_removing_runtime_changes_behavior(monkeypatch) -> None:
    """如果 AgentRuntime 不可用，RUNTIME 模式必须**停止工作**。

    Phase 5.5 变化：不再退化到 legacy，而是返回未处理 + 安全文案。
    这证明 AgentRuntime 是**必要**的，不是摆设。

    （对比 LEGACY 模式：那里 runtime 不可用也不影响，因为根本不走它。）
    """
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "runtime")

    from dl_senpai.agent.context import AgentContext
    from dl_senpai.agent.entry import AgentEntry
    from dl_senpai.gateway import MessageContext

    calls: list[str] = []

    async def legacy(ctx: AgentContext) -> Any:
        calls.append("legacy")
        return "legacy"

    msg = MessageContext(
        message_type="group", group_id="900001", user_id="10001",
        session_id="group:900001", plain_text="你好",
    )
    ctx = AgentContext(session_id="group:900001", message_context=msg)

    # 有 runtime：走新路径
    class RT:
        executor = None

        async def run(self, *a, **k):
            from dl_senpai.agent.context import AgentResult

            calls.append("runtime")
            return AgentResult(text="new")

    entry_with = AgentEntry(runtime=RT(), legacy_handler=legacy)
    await entry_with.handle(ctx)
    assert calls == ["runtime"], "有 runtime 时应走新路径"

    # 无 runtime：RUNTIME 模式**不再**退化到 legacy
    calls.clear()
    entry_without = AgentEntry(runtime=None, legacy_handler=legacy)
    outcome = await entry_without.handle(ctx)
    assert calls == [], "RUNTIME 模式不应 fallback 到 legacy（Phase 5.5）"
    assert outcome.handled is False
    assert outcome.error == "runtime_unavailable"
    assert entry_without.need_safe_fallback(outcome) is True


# ================================================================
# 3. 拿掉 ToolRegistry → 工具不可用
# ================================================================


def test_without_tool_registry_no_tools_visible() -> None:
    """空注册表 ⇒ 模型看不到任何工具（证明 prompt 由注册表驱动）。"""
    from dl_senpai.agent.context import AgentContext
    from dl_senpai.agent.tools.registry import ToolRegistry

    empty = ToolRegistry()
    ctx = AgentContext(session_id="s")
    assert empty.visible_tools(ctx) == []
    assert empty.markers() == {}


@pytest.mark.asyncio
async def test_without_tool_registry_no_tools_execute() -> None:
    """空注册表 ⇒ 工具调用无法执行。"""
    from dl_senpai.agent.context import AgentContext
    from dl_senpai.agent.tools.executor import ToolExecutor
    from dl_senpai.agent.tools.models import ToolCall
    from dl_senpai.agent.tools.registry import ToolRegistry

    ex = ToolExecutor(ToolRegistry())
    result = await ex.execute(ToolCall(tool_name="mute"), AgentContext(session_id="s"))
    assert result.success is False
    assert result.error_type == "unknown_tool"


# ================================================================
# 4. 拿掉 MarkerToolAdapter → marker 无法解析
# ================================================================


def test_without_adapter_no_marker_parsing() -> None:
    """用空注册表的 adapter 解析 marker ⇒ 解析不出任何调用。"""
    from dl_senpai.agent.tools.marker_adapter import MarkerToolAdapter
    from dl_senpai.agent.tools.registry import ToolRegistry

    adapter = MarkerToolAdapter(ToolRegistry())
    out = adapter.parse_response('<<<CARD {"user_id":1,"card":"x","kind":"request"}>>>')
    assert out.calls == []
    # marker 原文还在（没有 parser 去剥离它）
    assert "CARD" in out.text


# ================================================================
# 5. 生产 ToolRegistry 已填充（不是空壳）
# ================================================================


def test_production_registry_populated() -> None:
    from dl_senpai.agent.tools.builtin import build_production_tools

    tools = build_production_tools()
    # 2026-10-14：新增 group_read（读群公告/群文件/群相册）
    assert len(tools) == 9
    # 每个工具都必须是"可解析 + 可执行"的，否则只是空壳
    for tool in tools:
        assert tool.parser is not None, f"{tool.name} 缺 parser"
        assert tool.executor is not None, f"{tool.name} 缺 executor"
        assert tool.marker, f"{tool.name} 缺 marker"


def test_production_executors_are_real_not_deferred() -> None:
    """8 个 executor 必须都是真实实现，不再返回 deferred 占位。"""
    from dl_senpai.agent.tools.builtin import build_production_tools
    from dl_senpai.agent.tools.executors import REAL_EXECUTORS

    for tool in build_production_tools():
        assert tool.name in REAL_EXECUTORS, f"{tool.name} 没有真实 executor"
        assert tool.executor is REAL_EXECUTORS[tool.name], (
            f"{tool.name} 用的仍是占位 executor"
        )


# ================================================================
# 6. RUNTIME 成功后必须真的把回复发出去
# ================================================================


class _SendOutcome:
    """伪造 ResponsePipeline 的返回值（只需要 .sent / .error）。"""

    def __init__(self, text: str) -> None:
        self.sent = True
        self.text = text
        self.media: list[str] = []
        self.error = ""


def _tmp_cfg(tmp_path):
    """全部落盘目录指向 tmp_path：测试写盘必须落在临时目录。"""
    from dl_senpai.config import PluginConfig

    return PluginConfig(
        DL_SENPAI_MEMORY_DIR=str(tmp_path / "memory"),
        DL_SENPAI_INNER_STATE_DIR=str(tmp_path / "inner_state"),
        DL_SENPAI_AFFECTION_DIR=str(tmp_path / "affection"),
        DL_SENPAI_GROUP_STYLE_DIR=str(tmp_path / "group_style"),
        DL_SENPAI_RECENT_CHAT_DIR=str(tmp_path / "recent_chat"),
        DL_SENPAI_MEMORY_CARDS_DIR=str(tmp_path / "memory_cards"),
    )


class _FakeGatewayCtx:
    """够用的 gateway 上下文替身（handle_agent_message 只读这几个属性）。"""

    def __init__(self, config=None) -> None:
        self.session_id = "private:10001"
        self.raw_event = object()
        self.bot = object()
        # 带上 tmp 配置：不带的话 agent 路径会回退到真实 get_config()，
        # 回复落盘与内心状态结算就会写进生产 data/dl_senpai。
        self.extra: dict[str, Any] = {"config": config} if config is not None else {}
        self.is_private = True
        self.message_type = "private"
        self.group_id = ""
        self.user_id = "10001"
        self.plain_text = "死锁的四个必要条件"
        self.trace_id = "t-1"


def _install_delivery_spy(monkeypatch) -> list[str]:
    """把 ResponsePipeline 换成记录器，返回被发送的文本列表。"""
    from dl_senpai import legacy_bridge

    sent: list[str] = []

    class _FakePipeline:
        async def deliver(self, result, context, *, dry_run=False, reply_options=None):
            sent.append(result.text)
            return _SendOutcome(result.text)

    monkeypatch.setattr("dl_senpai.agent.response.ResponsePipeline", _FakePipeline)
    # 不真跑 SessionExecutor：这里只验证「发送有没有接线」
    monkeypatch.setattr(
        "dl_senpai.core.session.get_executor", lambda: None, raising=False
    )
    return sent


@pytest.mark.asyncio
async def test_runtime_success_reply_is_actually_sent(monkeypatch, tmp_path) -> None:
    """RUNTIME 成功时，回复必须真的发出去。

    回归测试（2026-10-11 线上故障）：桥接层当初只接了「基础设施失败 → 发
    安全文案」那一支，**成功分支没有接线**。症状极具迷惑性——日志里
    `agent.trigger` / `agent.entry` / `llm.usage` / `agent.finished` 全部正常，
    `reply_len` 也好端端的，却没有任何发送记录，用户一条回复都收不到，
    而且全程零报错，因为代码根本没走到发送。
    """
    from dl_senpai import legacy_bridge
    from dl_senpai.agent.context import AgentResult
    from dl_senpai.agent.entry import AgentOutcome

    sent = _install_delivery_spy(monkeypatch)

    class _FakeEntry:
        async def handle(self, context):
            return AgentOutcome(
                handled=True,
                result=AgentResult(text="死锁需要四个条件。"),
                mode="runtime",
                used_runtime=True,
                stop_reason="completed",
            )

        def need_safe_fallback(self, outcome) -> bool:
            return False

    monkeypatch.setattr(legacy_bridge, "_get_agent_entry", lambda: _FakeEntry())

    await legacy_bridge.handle_agent_message(_FakeGatewayCtx(_tmp_cfg(tmp_path)))
    assert sent == ["死锁需要四个条件。"], "RUNTIME 成功产出的回复没有被发送"


@pytest.mark.asyncio
async def test_legacy_path_does_not_double_send(monkeypatch, tmp_path) -> None:
    """LEGACY 路径自己已经发过，桥接层不得再发一遍。

    这条断言和上一条是一对：修好「runtime 漏发」时最容易顺手写出
    「两条路径都发」——那会让 legacy 群里的回复全部翻倍。
    """
    from dl_senpai import legacy_bridge
    from dl_senpai.agent.entry import AgentOutcome

    sent = _install_delivery_spy(monkeypatch)

    class _FakeEntry:
        async def handle(self, context):
            return AgentOutcome(
                handled=True,
                legacy_result="已由 legacy 发送",
                mode="legacy",
                used_runtime=False,
            )

        def need_safe_fallback(self, outcome) -> bool:
            return False

    monkeypatch.setattr(legacy_bridge, "_get_agent_entry", lambda: _FakeEntry())

    await legacy_bridge.handle_agent_message(_FakeGatewayCtx(_tmp_cfg(tmp_path)))
    assert sent == [], "LEGACY 路径被重复发送了一次"
