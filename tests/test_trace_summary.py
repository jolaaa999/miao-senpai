"""请求级 trace 摘要：每条消息落地一行可 grep 的结论。

要守住两件事：

1. **能回答现场问题**：进了哪条路、停了为什么、模型调了几次、花了多少 token、
   调了哪些工具各多久、最后谁生成的回复。
2. **不泄漏内容**：只写元数据。日志会被复制粘贴出去，
   用户原话、回复正文、记忆内容一律不能出现在这一行里。
"""

from __future__ import annotations

from typing import Any

import pytest

from dl_senpai.agent.context import AgentResult
from dl_senpai.agent.entry import AgentEntry
from dl_senpai.agent.guard import StopReason
from dl_senpai.agent.tools.registry import ToolDefinition, ToolRegistry
from dl_senpai.validation.telemetry import (
    ModelCallTelemetry,
    ToolOutcome,
    ToolTelemetry,
    record_model_call,
    record_tool_call,
)


class _FakeLogger:
    """收集 log_event 产出的整行文本。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def _record(self, message: str) -> None:
        self.lines.append(str(message))

    def info(self, message: str) -> None:
        self._record(message)

    warning = info
    error = info
    debug = info

    def find(self, event: str) -> list[str]:
        return [line for line in self.lines if line.startswith(event)]


@pytest.fixture()
def captured(monkeypatch):
    """替换 logging 模块的 sink（与既有 test_observability 同款做法）。"""
    import dl_senpai.observability.logging as logging_mod

    fake = _FakeLogger()
    monkeypatch.setattr(logging_mod, "_LOG", fake)
    return fake


class _Cfg:
    agent_trace_summary = True
    agent_admin_qq = ""


class _Ctx:
    """AgentContext 的最小替身。"""

    def __init__(self) -> None:
        self.session_id = "group:1"
        self.trace_id = "trace-abc"
        self.trigger_reason = "mention"
        self.group_id = "1"
        self.user_id = "7"
        self.actor = "小明"
        self.interrupt = False
        self.private_chat = False
        self.config = _Cfg()
        self.extra: dict[str, Any] = {}

    @property
    def plain_text(self) -> str:
        return "今天天气怎么样"


class _Runtime:
    """假 AgentRuntime：按真实路径写 telemetry，再返回一个结果。"""

    def __init__(self) -> None:
        self.registry = ToolRegistry()
        self.registry.register(ToolDefinition(name="search", description="联网检索"))

    async def run(self, context: Any, journal: Any = None, **_kw: Any) -> AgentResult:
        # 走的是生产同款采集入口：ToolExecutor / LLM 就是这么写的
        record_tool_call(
            ToolTelemetry(
                tool="search",
                call_id="search-1",
                outcome=ToolOutcome.SUCCESS.value,
                duration_ms=123.4,
            )
        )
        record_model_call(
            ModelCallTelemetry(
                model="gpt-6-astra",
                call_index=0,
                latency_ms=800.0,
                total_tokens=1234,
            )
        )
        return AgentResult(text="还行", steps=1, stop_reason=StopReason.FINISHED)


async def test_summary_reports_the_whole_chain(captured) -> None:
    entry = AgentEntry(runtime=_Runtime())
    await entry.handle(_Ctx())

    lines = captured.find("agent.request_summary")
    assert len(lines) == 1, f"应恰好一行摘要，实际: {lines}"
    line = lines[0]
    # -- 谁、走哪条路、为什么停 --
    assert "trace=trace-abc" in line
    assert "session_id=group:1" in line
    assert "mode=runtime" in line
    assert "reply_by=runtime" in line
    assert "trigger=mention" in line
    assert "stop=finished" in line
    # -- 花了多少 --
    assert "model_calls=1" in line
    assert "usage=1234" in line
    # -- 调了什么、多久 --
    assert "search:success:" in line
    assert "tool_calls=1" in line
    # -- 能力集：这一轮模型能看见什么 --
    assert "capabilities=" in line
    assert "sources=builtin:1" in line


async def test_summary_never_leaks_content(captured) -> None:
    """日志会被复制粘贴出去：用户原话与回复正文都不许出现。"""
    entry = AgentEntry(runtime=_Runtime())
    await entry.handle(_Ctx())
    line = captured.find("agent.request_summary")[0]
    assert "今天天气" not in line
    assert "还行" not in line
    assert "小明" not in line


async def test_summary_marks_safe_fallback_when_runtime_missing(captured) -> None:
    """Agent 层构建失败时，摘要必须能直接看出"这条是兜底文案"。"""
    entry = AgentEntry(runtime=None)
    await entry.handle(_Ctx())
    line = captured.find("agent.request_summary")[0]
    assert "reply_by=safe_fallback" in line
    assert "stop=infra_error" in line
    assert "error=runtime_unavailable" in line


async def test_summary_can_be_switched_off(captured, monkeypatch) -> None:
    class _Off(_Cfg):
        agent_trace_summary = False

    ctx = _Ctx()
    ctx.config = _Off()
    await AgentEntry(runtime=_Runtime()).handle(ctx)
    assert captured.find("agent.request_summary") == []


async def test_summary_survives_failing_runtime(captured) -> None:
    """runtime 崩了也必须有摘要，否则出故障时恰恰最缺证据。"""

    class _Boom(_Runtime):
        async def run(self, context: Any, journal: Any = None, **_kw: Any):
            raise RuntimeError("kaboom")

    await AgentEntry(runtime=_Boom()).handle(_Ctx())
    line = captured.find("agent.request_summary")[0]
    assert "reply_by=safe_fallback" in line
    assert "stop=infra_error" in line
