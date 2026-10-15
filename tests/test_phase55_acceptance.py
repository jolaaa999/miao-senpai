"""Phase 5.5 强验收测试（第四十七/四十八/四十九节）。

三个强标准：

47. RUNTIME 生产调用链不得引用 `_handle_chat_inner`
48. monkeypatch `_handle_chat_inner` 抛异常后，RUNTIME integration tests 仍全通过
49. LEGACY 模式下 monkeypatch `AgentRuntime.run` 抛异常，回滚仍工作

这些比"看代码觉得已经切换"可靠得多。
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from dl_senpai.agent.context import AgentContext, AgentResult
from dl_senpai.agent.entry import AgentEntry
from dl_senpai.agent.model_gateway import StubModelGateway
from dl_senpai.agent.runtime import AgentRuntime
from dl_senpai.agent.tools.builtin import register_production_tools
from dl_senpai.agent.tools.registry import ToolRegistry


def make_ctx(**kw: Any) -> AgentContext:
    from dl_senpai.gateway import MessageContext

    base = dict(
        message_type="group", group_id="900001", user_id="10001",
        session_id="group:900001", plain_text="你好",
    )
    base.update(kw)
    msg = MessageContext(**base)
    return AgentContext(
        session_id=msg.session_id, message_context=msg, trace_id="t",
    )


def make_runtime(replies: list[str] | None = None) -> AgentRuntime:
    registry = ToolRegistry()
    register_production_tools(registry)
    for tool in registry.list():
        tool.executor = lambda c, k: None  # 不产生副作用
    return AgentRuntime(registry=registry, gateway=StubModelGateway(replies or ["在呢～"]))


# ================================================================
# 标准 47：RUNTIME 调用链不得引用 _handle_chat_inner
# ================================================================


def test_runtime_call_chain_has_no_handle_chat_inner_reference() -> None:
    """AgentRuntime 及其依赖组件不得**在代码中**引用 _handle_chat_inner。

    检查范围：agent/ 全部模块 + core/runtime.py。

    只看真实代码（AST 节点），不看 docstring —— 文档里需要解释
    "legacy 只允许出现在 bridge" 这类约束。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "plugins" / "dl_senpai"
    targets = list((root / "agent").rglob("*.py")) + [root / "core" / "runtime.py"]

    banned = {"_handle_chat_inner", "_handle_chat"}
    offenders: list[str] = []
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # 属性访问：obj._handle_chat(...)
            if isinstance(node, ast.Attribute) and node.attr in banned:
                offenders.append(f"{path.name}:{node.lineno} attr {node.attr}")
            # 名字引用：from x import _handle_chat / 直接调用
            if isinstance(node, ast.Name) and node.id in banned:
                offenders.append(f"{path.name}:{node.lineno} name {node.id}")
            # import 语句
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in banned:
                        offenders.append(
                            f"{path.name}:{node.lineno} import {alias.name}"
                        )

    assert not offenders, "RUNTIME 调用链引用了 legacy handler:\n" + "\n".join(offenders)


def test_entry_does_not_know_legacy_internals() -> None:
    """AgentEntry 不得知道 legacy 的实现细节（第十节）。

    用 AST 检查真实代码中的属性名与参数名，忽略 docstring。
    """
    import ast

    from dl_senpai.agent import entry as entry_mod

    tree = ast.parse(inspect.getsource(entry_mod))
    banned = {"_handle_chat_inner", "_handle_chat", "force_mention", "raw_event"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in banned:
            offenders.append(f"attr {node.attr} @{node.lineno}")
        if isinstance(node, ast.Name) and node.id in banned:
            offenders.append(f"name {node.id} @{node.lineno}")
        if isinstance(node, ast.keyword) and node.arg in banned:
            offenders.append(f"kwarg {node.arg} @{node.lineno}")
    assert not offenders, f"AgentEntry 泄漏了 legacy 细节: {offenders}"


def test_entry_only_has_allowed_responsibilities() -> None:
    """AgentEntry 只负责：mode / canary / shadow / runtime 调用 / error boundary。

    不得重新塞入 Prompt / Tool / Memory / Recovery 逻辑（第二十三节）。

    Phase 7A.1：改为检查**真实代码**（AST），不看 docstring ——
    注释里会提到 MemoryFacade 等名字来说明"这些不属于这里"。
    另外 `RequestTelemetry` 绑定是 Phase 7A.1 引入的**观测**职责，
    不属于编排，因此不在禁止之列。
    """
    import ast

    from dl_senpai.agent import entry as entry_mod

    tree = ast.parse(inspect.getsource(entry_mod))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])

    banned = (
        "PersonMemory", "Affection",
        "ToolExecutor", "MarkerToolAdapter", "PromptBuilder",
        "looks_like_card_request", "looks_like_mute_request",
    )
    for token in banned:
        assert token not in names, f"AgentEntry 侵入了不属于它的职责: {token}"


# ================================================================
# 标准 48：monkeypatch _handle_chat_inner 抛异常，RUNTIME 仍正常
# ================================================================


@pytest.mark.asyncio
async def test_runtime_works_with_handle_chat_inner_broken(monkeypatch) -> None:
    """把 legacy 打成废的，RUNTIME 路径必须照常工作。"""
    import dl_senpai.handlers as handlers

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("legacy agent is intentionally broken")

    monkeypatch.setattr(handlers, "_handle_chat", _boom, raising=False)
    monkeypatch.setattr(handlers, "_handle_chat_inner", _boom, raising=False)

    entry = AgentEntry(runtime=make_runtime(), legacy_handler=None)
    outcome = await entry.handle(make_ctx())

    assert outcome.used_runtime is True
    assert outcome.handled is True
    assert outcome.fallback_used is False
    assert outcome.result is not None
    assert outcome.result.text


@pytest.mark.asyncio
async def test_runtime_handles_tool_call_with_broken_legacy(monkeypatch) -> None:
    """带工具调用的完整链路也不依赖 legacy。"""
    import dl_senpai.handlers as handlers

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("legacy broken")

    monkeypatch.setattr(handlers, "_handle_chat", _boom, raising=False)
    monkeypatch.setattr(handlers, "_handle_chat_inner", _boom, raising=False)

    rt = make_runtime(
        ["<<<SEARCH 今日天气>>>", "今天晴～"]
    )
    # 给 search 一个能回填结果的 executor
    from dl_senpai.agent.tools.models import Continuation, ToolResult
    from dl_senpai.agent.tools.registry import ToolDefinition

    rt.registry.register(
        ToolDefinition(
            name="search", marker="SEARCH",
            parser=rt.registry.get("search").parser,
            executor=lambda c, k: ToolResult(
                tool_name="search", success=True, output="【联网检索结果】今天晴"
            ),
            continuation=Continuation.RETURN_TO_MODEL,
            priority=40,
        )
    )

    outcome = await AgentEntry(runtime=rt, legacy_handler=None).handle(make_ctx())
    assert outcome.used_runtime is True
    assert outcome.result.text == "今天晴～"
    assert len(outcome.result.tool_calls) == 1


@pytest.mark.asyncio
async def test_runtime_crash_with_broken_legacy_returns_safe(monkeypatch) -> None:
    """runtime 崩溃 + legacy 也是废的 → 必须优雅返回，不抛异常。"""
    import dl_senpai.handlers as handlers

    def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("legacy broken")

    monkeypatch.setattr(handlers, "_handle_chat", _boom, raising=False)

    class Crash:
        executor = None

        async def run(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("runtime broken")

    entry = AgentEntry(runtime=Crash(), legacy_handler=None)
    outcome = await entry.handle(make_ctx())
    assert outcome.handled is False
    assert outcome.error
    assert entry.need_safe_fallback(outcome) is True


# ================================================================
# 标准 49：LEGACY 模式下 runtime 坏掉，回滚仍工作
# ================================================================


@pytest.mark.asyncio
async def test_legacy_mode_works_with_runtime_broken(monkeypatch) -> None:
    """LEGACY 模式不依赖 AgentRuntime，runtime 坏掉也要能用。"""
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "legacy")

    calls: list[str] = []

    async def legacy(ctx: AgentContext) -> Any:
        calls.append("legacy")
        return "legacy-reply"

    class Crash:
        executor = None

        async def run(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("runtime broken")

    entry = AgentEntry(runtime=Crash(), legacy_handler=legacy)
    outcome = await entry.handle(make_ctx())

    assert calls == ["legacy"]
    assert outcome.used_runtime is False
    assert outcome.mode == "legacy"


@pytest.mark.asyncio
async def test_two_modes_are_independent(monkeypatch) -> None:
    """RUNTIME 与 LEGACY 是两个明确模式，不互相偷偷 fallback。"""
    calls: list[str] = []

    async def legacy(ctx: AgentContext) -> Any:
        calls.append("legacy")
        return "L"

    # RUNTIME + runtime 正常 → 不碰 legacy
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "runtime")
    await AgentEntry(runtime=make_runtime(), legacy_handler=legacy).handle(make_ctx())
    assert calls == [], "RUNTIME 正常路径不应碰 legacy"

    # RUNTIME + runtime 崩溃 → 也不碰 legacy
    calls.clear()

    class Crash:
        executor = None

        async def run(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("x")

    await AgentEntry(runtime=Crash(), legacy_handler=legacy).handle(make_ctx())
    assert calls == [], "RUNTIME 崩溃路径也不应碰 legacy"

    # LEGACY → 只碰 legacy
    calls.clear()
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "legacy")
    await AgentEntry(runtime=make_runtime(), legacy_handler=legacy).handle(make_ctx())
    assert calls == ["legacy"]


# ================================================================
# 标准 49 补充：SHADOW 仍调用统一 Runtime components
# ================================================================


@pytest.mark.asyncio
async def test_shadow_uses_unified_runtime_dry_run(monkeypatch) -> None:
    """SHADOW 模式：legacy 执行 + runtime 只算（dry_run）。"""
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "shadow")

    calls: list[str] = []

    async def legacy(ctx: AgentContext) -> Any:
        calls.append("legacy")
        return "L"

    class Spy:
        executor = None
        dry_runs = 0

        async def run(self, *a: Any, **k: Any) -> Any:
            if k.get("dry_run"):
                self.dry_runs += 1
            return AgentResult(text="shadow")

    spy = Spy()
    outcome = await AgentEntry(runtime=spy, legacy_handler=legacy).handle(make_ctx())
    assert calls == ["legacy"]
    assert spy.dry_runs == 1
    assert outcome.mode == "shadow"


@pytest.mark.asyncio
async def test_legacy_mode_emits_deprecation(monkeypatch) -> None:
    """LEGACY 模式必须能被识别为 deprecated（第二十节）。"""
    from dl_senpai.agent.mode import (
        AgentExecutionMode,
        deprecation_warning,
        is_deprecated_mode,
    )

    assert is_deprecated_mode(AgentExecutionMode.LEGACY) is True
    assert is_deprecated_mode(AgentExecutionMode.RUNTIME) is False
    warning = deprecation_warning(AgentExecutionMode.LEGACY)
    assert "deprecated" in warning
    assert deprecation_warning(AgentExecutionMode.RUNTIME) == ""
