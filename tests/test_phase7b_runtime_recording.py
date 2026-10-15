"""Phase 7B：RUNTIME 模式必须记录自身运行数据。

**修复的真实缺陷**：
    `_record_shadow_validation` 原先**只在 SHADOW 路径**调用。
    切到 RUNTIME 后采集完全停止 —— `real traffic samples` 永远不涨，
    报告里的 Queue / Model / Tool / Memory / Send / Latency 全是空。

    RUNTIME 是生产默认模式，它恰恰是**最需要**观测的那条路径。

这些测试锁死行为：runtime 模式也要落盘，且不伪造 legacy 对比数据。
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from dl_senpai.agent.context import AgentContext, AgentResult
from dl_senpai.agent.entry import AgentEntry
from dl_senpai.gateway import MessageContext
from dl_senpai.validation import ValidationStore, reset_recorder
from dl_senpai.validation import recorder as recorder_mod


def make_context(**kw: Any) -> AgentContext:
    base = dict(
        message_type="group", group_id="12345", user_id="67890",
        session_id="group:12345", plain_text="你好呀",
    )
    base.update(kw)
    msg = MessageContext(**base)
    return AgentContext(
        session_id=msg.session_id, message_context=msg,
        trigger_reason="mention", trace_id="tr-runtime",
    )


@pytest.fixture
def wired(tmp_path, monkeypatch):
    store = ValidationStore(tmp_path / "v", enabled=True)
    recorder = recorder_mod.ValidationRecorder(store)
    monkeypatch.setattr(recorder_mod, "_recorder", recorder)
    import dl_senpai.validation as vpkg

    monkeypatch.setattr(vpkg, "get_recorder", lambda: recorder)
    yield store, recorder
    reset_recorder()


@pytest.mark.asyncio
async def test_runtime_mode_records_validation(wired, monkeypatch) -> None:
    """核心回归：RUNTIME 模式必须产出验证记录。"""
    store, recorder = wired
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "runtime")

    class Runtime:
        executor = None

        async def run(self, ctx, **kw):
            from dl_senpai.validation.telemetry import (
                ModelCallTelemetry,
                record_model_call,
            )

            record_model_call(
                ModelCallTelemetry(
                    model="fake", latency_ms=200,
                    prompt_tokens=100, completion_tokens=50, total_tokens=150,
                )
            )
            result = AgentResult(text="runtime 回复")
            result.tool_calls = []
            return result

    entry = AgentEntry(runtime=Runtime(), legacy_handler=None)
    await entry.handle(make_context())

    rows = store.read()
    assert len(rows) == 1, f"RUNTIME 模式未记录: {rows}"
    row = rows[0]
    assert row["execution_mode"] == "runtime"
    # runtime 侧数据存在
    assert row["runtime"] is not None
    assert row["runtime"]["model_call_count"] == 1
    # legacy 侧为 None（不做对比）
    assert row["legacy"] is None
    # 没有对比就不该有 mismatch
    assert row["mismatches"] == []


@pytest.mark.asyncio
async def test_runtime_records_latency_and_metrics(wired, monkeypatch) -> None:
    """延迟 / token / 工具等字段在 runtime 模式也要有值。"""
    store, recorder = wired
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "runtime")

    class Runtime:
        executor = None

        async def run(self, ctx, **kw):
            from dl_senpai.validation.telemetry import (
                ModelCallTelemetry,
                ToolTelemetry,
                record_model_call,
                record_memory,
                record_send,
                record_tool_call,
            )

            record_model_call(
                ModelCallTelemetry(
                    model="m", latency_ms=300, total_tokens=200,
                    prompt_tokens=150, completion_tokens=50,
                )
            )
            record_tool_call(
                ToolTelemetry(tool="search", call_id="c1", outcome="success",
                              duration_ms=80)
            )
            record_memory(history_count=6, person_profile_hit=True)
            record_send(success=True, operations=1, bubbles=1, duration_ms=30)
            result = AgentResult(text="ok")
            result.tool_calls = []
            return result

    entry = AgentEntry(runtime=Runtime(), legacy_handler=None)
    await entry.handle(make_context())

    row = store.read()[0]
    assert row["latency"]["total"] is not None
    assert row["runtime"]["total_tokens"] == 200
    assert row["tools"], "工具明细缺失"
    assert row["tools"][0]["tool"] == "search"
    assert row["memory"]["history_count"] == 6
    assert row["send"]["send_success"] == 1
    assert row["model_calls"], "模型调用明细缺失"


@pytest.mark.asyncio
async def test_runtime_recording_is_privacy_safe(wired, monkeypatch) -> None:
    """runtime 模式的记录同样不得含明文。"""
    store, recorder = wired
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "runtime")

    class Runtime:
        executor = None

        async def run(self, ctx, **kw):
            result = AgentResult(text="不会落盘的回复")
            result.tool_calls = []
            return result

    entry = AgentEntry(runtime=Runtime(), legacy_handler=None)
    await entry.handle(
        make_context(
            group_id="998877665", user_id="112233445",
            session_id="group:998877665",
            plain_text="我的手机号13800138000",
        )
    )

    raw = next((store.directory).glob("*.jsonl")).read_text(encoding="utf-8")
    for secret in ("998877665", "112233445", "13800138000",
                   "我的手机号", "不会落盘的回复"):
        assert secret not in raw, f"runtime 记录泄漏了 {secret!r}"


@pytest.mark.asyncio
async def test_runtime_recording_failure_does_not_break(wired, monkeypatch) -> None:
    """观测失败不影响 runtime 业务。"""
    store, recorder = wired
    monkeypatch.setenv("DL_SENPAI_AGENT_MODE", "runtime")

    def boom(*a, **k):
        raise RuntimeError("validation exploded")

    monkeypatch.setattr(recorder, "record_request", boom)

    class Runtime:
        executor = None

        async def run(self, ctx, **kw):
            result = AgentResult(text="正常回复")
            result.tool_calls = []
            return result

    entry = AgentEntry(runtime=Runtime(), legacy_handler=None)
    outcome = await entry.handle(make_context())
    assert outcome.handled is True
    assert outcome.result.text == "正常回复"


def test_runtime_recorder_exists_in_entry_module() -> None:
    """结构断言：runtime 路径必须有记录调用（防止被误删）。"""
    import inspect

    from dl_senpai.agent import entry as entry_mod

    src = inspect.getsource(entry_mod)
    assert "_record_runtime_validation" in src
    # 必须在 _run_runtime 内被调用
    runtime_src = inspect.getsource(entry_mod.AgentEntry._run_runtime)
    assert "_record_runtime_validation" in runtime_src, (
        "RUNTIME 路径未接入验证记录（采集会停止）"
    )
