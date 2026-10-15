"""护栏预算配置化 + 工具并发执行 的回归测试。

这两项都是"通用 agent 能力补齐"的产物：
- 护栏预算（max_steps / timeout / token 预算）从 .env 读取，支持热重载；
- 同一轮回复里的多个工具调用改为并发执行。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from dl_senpai.agent.context import AgentContext
from dl_senpai.agent.guard import AgentGuard, GuardConfig
from dl_senpai.agent.model_gateway import StubModelGateway
from dl_senpai.agent.runtime import AgentRuntime
from dl_senpai.agent.tools.models import Continuation, ToolCall, ToolResult
from dl_senpai.agent.tools.registry import ToolDefinition, ToolRegistry


def _ctx(**kw: Any) -> AgentContext:
    base = dict(session_id="group:1", trace_id="t1", trigger_reason="mention")
    base.update(kw)
    return AgentContext(**base)


class _Config:
    """最小配置替身：只带护栏预算字段。"""

    def __init__(self, **kw: Any) -> None:
        self.agent_max_steps = kw.get("agent_max_steps", 4)
        self.agent_max_tool_calls = kw.get("agent_max_tool_calls", 8)
        self.agent_same_tool_repeat_limit = kw.get("agent_same_tool_repeat_limit", 2)
        self.agent_timeout = kw.get("agent_timeout", 90.0)
        self.agent_token_budget = kw.get("agent_token_budget", 0)


# ================================================================
# 护栏预算：从配置读取（而不是写死 GuardConfig 默认值）
# ================================================================


def test_guard_config_defaults_when_no_config() -> None:
    rt = AgentRuntime(registry=ToolRegistry(), gateway=StubModelGateway())
    cfg = rt._resolve_guard_config(_ctx())
    assert cfg.max_steps == 4
    assert cfg.max_tool_calls == 8
    assert cfg.timeout == 90.0
    assert cfg.token_budget is None


def test_guard_config_reads_from_context_config() -> None:
    rt = AgentRuntime(registry=ToolRegistry(), gateway=StubModelGateway())
    cfg = rt._resolve_guard_config(
        _ctx(config=_Config(agent_max_steps=20, agent_max_tool_calls=30, agent_timeout=300.0))
    )
    assert cfg.max_steps == 20
    assert cfg.max_tool_calls == 30
    assert cfg.timeout == 300.0


def test_guard_token_budget_zero_means_unlimited() -> None:
    """0 是"不限制"，必须翻译成 None——否则预算 0 会把第一轮就掐死。"""
    rt = AgentRuntime(registry=ToolRegistry(), gateway=StubModelGateway())
    assert rt._resolve_guard_config(_ctx(config=_Config(agent_token_budget=0))).token_budget is None
    assert rt._resolve_guard_config(_ctx(config=_Config(agent_token_budget=5000))).token_budget == 5000


def test_explicit_guard_config_wins() -> None:
    """显式注入的护栏（测试/shadow）优先级最高，保证可复现。"""
    injected = GuardConfig(max_steps=99)
    rt = AgentRuntime(
        registry=ToolRegistry(), gateway=StubModelGateway(), guard_config=injected
    )
    assert rt._resolve_guard_config(_ctx(config=_Config(agent_max_steps=1))) is injected


def test_bad_config_falls_back_to_defaults() -> None:
    """配置读坏了也不能让整轮对话崩掉。"""

    class _Broken:
        @property
        def agent_max_steps(self) -> int:
            raise ValueError("boom")

    rt = AgentRuntime(registry=ToolRegistry(), gateway=StubModelGateway())
    cfg = rt._resolve_guard_config(_ctx(config=_Broken()))
    assert cfg.max_steps == 4


@pytest.mark.asyncio
async def test_runtime_honours_configured_max_steps() -> None:
    """max_steps=1 时，模型第二条要工具的回复不该被执行第二次。"""
    def _parse(text: str):
        # 关键：没有 marker 时必须返回空调用列表，否则"收尾回复"会被
        # 当成又一次工具请求，把循环预算测试测成别的东西。
        if "<<<ECHO>>>" not in text:
            return text, []
        return text.replace("<<<ECHO>>>", ""), [ToolCall(tool_name="echo")]

    reg = ToolRegistry()
    reg.register(
        ToolDefinition(
            name="echo",
            marker="ECHO",
            parser=_parse,
            executor=lambda c, k: ToolResult(tool_name="echo", success=True, output="ok"),
            continuation=Continuation.RETURN_TO_MODEL,
        )
    )
    gw = StubModelGateway(["<<<ECHO>>>", "<<<ECHO>>>", "<<<ECHO>>>"])
    rt = AgentRuntime(registry=reg, gateway=gw)
    result = await rt.run(
        _ctx(config=_Config(agent_max_steps=1)), "hi"
    )
    # 只允许一轮 → 只发出一次模型调用
    assert len(gw.requests) == 1
    assert result.steps == 1


# ================================================================
# 并发执行：同一轮的多个工具必须同时开始
# ================================================================


def _slow_registry(started: list[str], delay: float = 0.2) -> ToolRegistry:
    """两个不同工具，各自记录"开始执行的时刻"。"""

    def _make(name: str):
        async def _run(_c: Any, _k: Any) -> ToolResult:
            started.append(name)
            await asyncio.sleep(delay)
            return ToolResult(tool_name=name, success=True, output=f"{name}-ok")

        return _run

    def _parser_for(name: str):
        marker = f"<<<{name.upper()}>>>"

        def _parse(text: str):
            # 无 marker 时必须返回空列表，否则收尾的普通回复会被反复当工具调用
            if marker not in text:
                return text, []
            return text.replace(marker, ""), [ToolCall(tool_name=name)]

        return _parse

    reg = ToolRegistry()
    for name in ("alpha", "beta"):
        reg.register(
            ToolDefinition(
                name=name,
                marker=name.upper(),
                parser=_parser_for(name),
                executor=_make(name),
                continuation=Continuation.RETURN_TO_MODEL,
            )
        )
    return reg


@pytest.mark.asyncio
async def test_same_round_tools_run_concurrently() -> None:
    """两个各 0.2s 的工具，并发执行总耗时应显著小于 0.4s（串行）。"""
    started: list[str] = []
    reg = _slow_registry(started, delay=0.25)
    gw = StubModelGateway(["<<<ALPHA>>>\n<<<BETA>>>", "done"])
    rt = AgentRuntime(registry=reg, gateway=gw)

    t0 = time.perf_counter()
    result = await rt.run(_ctx(), "hi")
    elapsed = time.perf_counter() - t0

    assert {r.tool_name for r in result.tool_results} == {"alpha", "beta"}
    assert elapsed < 0.45, f"看起来是串行执行: {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_same_round_results_keep_original_order() -> None:
    """并发执行不能打乱结果顺序——回填给模型时顺序必须与调用一致。"""
    started: list[str] = []
    reg = _slow_registry(started, delay=0.01)
    gw = StubModelGateway(["<<<ALPHA>>>\n<<<BETA>>>", "done"])
    rt = AgentRuntime(registry=reg, gateway=gw)
    result = await rt.run(_ctx(), "hi")
    assert [r.tool_name for r in result.tool_results] == ["alpha", "beta"]
    assert [c.tool_name for c in result.tool_calls] == ["alpha", "beta"]


@pytest.mark.asyncio
async def test_repeat_limit_still_enforced_in_parallel_batch() -> None:
    """并发改造后，"同工具重复上限"必须仍然生效。

    这是最容易出错的地方：若先并发执行再逐个记账，同一批里的
    两个相同调用会互相看不见对方的计数，从而绕过上限。
    """
    calls: list[str] = []

    def _exec(_c: Any, _k: Any) -> ToolResult:
        calls.append("x")
        return ToolResult(tool_name="x", success=True, output="ok")

    def _parse(text: str):
        if "<<<X>>>" not in text:
            return text, []
        # 必须是三个**独立对象**：列表乘法会复用同一个 ToolCall（同一个 id），
        # 那样第二个就会被执行期的幂等去重拦掉，测不到护栏本身。
        return text.replace("<<<X>>>", ""), [ToolCall(tool_name="x") for _ in range(3)]

    reg = ToolRegistry()
    reg.register(
        ToolDefinition(
            name="x",
            marker="X",
            parser=_parse,
            executor=_exec,
            continuation=Continuation.RETURN_TO_MODEL,
        )
    )
    # 一条回复里写 3 个同工具同参数调用，上限是 2 → 第 3 个应被拦
    gw = StubModelGateway(["<<<X>>>", "done"])
    rt = AgentRuntime(registry=reg, gateway=gw)
    result = await rt.run(_ctx(), "hi")
    assert len(calls) == 2, f"重复上限被绕过，实际执行了 {len(calls)} 次"


@pytest.mark.asyncio
async def test_guard_records_all_allowed_calls() -> None:
    started: list[str] = []
    reg = _slow_registry(started, delay=0.01)
    gw = StubModelGateway(["<<<ALPHA>>>\n<<<BETA>>>", "done"])
    rt = AgentRuntime(registry=reg, gateway=gw)
    result = await rt.run(_ctx(), "hi")
    assert result.steps >= 1
    assert len(result.tool_results) == 2
