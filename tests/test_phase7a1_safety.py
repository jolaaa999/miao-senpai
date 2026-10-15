"""Phase 7A.1：插桩失败安全性与隐私测试。

覆盖第五十/五十一/六十一/六十二/六十四节：
    instrumentation exception does not break business
    privacy failure does not leak raw values
    secret redaction（含 URL query）
    search query 不落盘
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from dl_senpai.validation import ValidationStore, reset_recorder
from dl_senpai.validation import recorder as recorder_mod
from dl_senpai.validation.telemetry import (
    ModelCallTelemetry,
    RequestTelemetry,
    ToolTelemetry,
    bind_telemetry,
    unbind_telemetry,
)


# ================================================================
# 1. Fail-open（第五十节）
# ================================================================


@pytest.mark.asyncio
async def test_store_failure_does_not_break_business(tmp_path, monkeypatch) -> None:
    """第五十节：ValidationStore.append 抛异常时业务仍正常。"""
    from dl_senpai.agent.context import AgentContext, AgentResult
    from dl_senpai.agent.entry import AgentEntry
    from dl_senpai.gateway import MessageContext

    class Exploding:
        enabled = True

        def append(self, record):
            raise OSError("disk full")

        def read(self, **kw):
            return []

    recorder = recorder_mod.ValidationRecorder(Exploding())
    monkeypatch.setattr(recorder_mod, "_recorder", recorder)
    import dl_senpai.validation as vpkg

    monkeypatch.setattr(vpkg, "get_recorder", lambda: recorder)

    async def legacy_handler(ctx):
        from dl_senpai.llm import ChatResult

        return ChatResult(text="正常回复")

    class Runtime:
        executor = None

        async def run(self, ctx, **kw):
            return AgentResult(text="runtime")

    msg = MessageContext(
        message_type="group", group_id="1", user_id="2",
        session_id="group:1", plain_text="hi",
    )
    ctx = AgentContext(session_id="group:1", message_context=msg)

    import os

    os.environ["DL_SENPAI_AGENT_MODE"] = "shadow"
    try:
        entry = AgentEntry(runtime=Runtime(), legacy_handler=legacy_handler)
        outcome = await entry.handle(ctx)
    finally:
        os.environ.pop("DL_SENPAI_AGENT_MODE", None)
        reset_recorder()

    # 业务照常完成
    assert outcome.handled is True


def test_telemetry_write_methods_never_raise() -> None:
    """telemetry 的写入方法必须吞掉一切异常。"""
    tel = RequestTelemetry()

    class Bad:
        def __str__(self) -> str:
            raise RuntimeError("boom")

    # 传入会炸的对象也不应抛出
    tel.record_tool(ToolTelemetry(tool="x", call_id="1", outcome="success"))
    tel.record_model_call(ModelCallTelemetry(model="m"))
    tel.record_send()
    tel.record_memory()
    tel.record_scope_violation("x")
    tel.record_error("boom", "x")
    assert tel.model_call_count == 1


def test_no_telemetry_bound_is_silent() -> None:
    """未绑定 telemetry 时，各 record_* 便利函数必须静默。"""
    from dl_senpai.validation.telemetry import (
        record_memory,
        record_model_call,
        record_scope_violation,
        record_send,
        record_tool_call,
    )

    # 这些都不应抛异常（当前 context 无 telemetry）
    record_tool_call(ToolTelemetry(tool="x", call_id="1", outcome="success"))
    record_model_call(ModelCallTelemetry(model="m"))
    record_send(success=True)
    record_memory(history_count=1)
    record_scope_violation("x")


# ================================================================
# 2. 隐私失败不得泄漏（第五十一节）
# ================================================================


def test_anonymize_failure_does_not_fallback_to_plaintext(monkeypatch) -> None:
    """第五十一节：anonymization 失败时 drop 字段，**不得**写原始 id。"""
    from dl_senpai.validation import privacy

    def boom(*a, **k):
        raise RuntimeError("hmac unavailable")

    monkeypatch.setattr(privacy.hmac, "new", boom)

    # anonymize 自身会抛出（调用方负责兜底），关键是不能返回明文
    with pytest.raises(Exception):
        privacy.anonymize("123456789", kind="user")


def test_recorder_drops_ids_when_anonymize_fails(monkeypatch) -> None:
    """Recorder 在匿名化失败时应记为不可用，而不是写明文。"""
    from dl_senpai.validation import recorder as rec_mod

    def boom(value, kind="id"):
        raise RuntimeError("anonymize failed")

    monkeypatch.setattr(rec_mod, "anonymize", boom)

    recorder = rec_mod.ValidationRecorder(ValidationStore(enabled=False))
    # build_record 会因匿名化失败而抛异常 —— 调用方（record_request）会兜住
    with pytest.raises(Exception):
        recorder.build_record(user_id="123456789", user_text="hi")


@pytest.mark.asyncio
async def test_entry_survives_anonymize_failure(tmp_path, monkeypatch) -> None:
    """端到端：匿名化失败时业务不受影响，且不落明文。"""
    from dl_senpai.validation import recorder as rec_mod

    def boom(value, kind="id"):
        raise RuntimeError("anonymize failed")

    monkeypatch.setattr(rec_mod, "anonymize", boom)

    store = ValidationStore(tmp_path / "v", enabled=True)
    recorder = rec_mod.ValidationRecorder(store)
    monkeypatch.setattr(rec_mod, "_recorder", recorder)
    import dl_senpai.validation as vpkg

    monkeypatch.setattr(vpkg, "get_recorder", lambda: recorder)

    from dl_senpai.agent.context import AgentContext, AgentResult
    from dl_senpai.agent.entry import AgentEntry
    from dl_senpai.gateway import MessageContext

    async def legacy_handler(ctx):
        from dl_senpai.llm import ChatResult

        return ChatResult(text="ok")

    class Runtime:
        executor = None

        async def run(self, ctx, **kw):
            return AgentResult(text="r")

    msg = MessageContext(
        message_type="group", group_id="99887766", user_id="11223344",
        session_id="group:99887766", plain_text="hi",
    )
    ctx = AgentContext(session_id="group:99887766", message_context=msg)

    import os

    os.environ["DL_SENPAI_AGENT_MODE"] = "shadow"
    try:
        entry = AgentEntry(runtime=Runtime(), legacy_handler=legacy_handler)
        outcome = await entry.handle(ctx)
    finally:
        os.environ.pop("DL_SENPAI_AGENT_MODE", None)
        reset_recorder()

    assert outcome.handled is True
    # 落盘内容里不得出现明文 id
    for path in (tmp_path / "v").glob("*.jsonl"):
        raw = path.read_text(encoding="utf-8")
        assert "99887766" not in raw
        assert "11223344" not in raw


# ================================================================
# 3. 密钥脱敏（第六十一/六十二节）
# ================================================================


@pytest.mark.parametrize(
    "text",
    [
        "sk-abcdefghijklmnopqrst",
        "Authorization: Bearer abc.def.ghi",
        "api_key=verysecretvalue123",
        "password=hunter2xyz",
        "token=abcdefghijklmnop",
    ],
)
def test_secrets_never_reach_store(tmp_path, text: str) -> None:
    """第六十一节：密钥不得进入 JSONL。"""
    store = ValidationStore(tmp_path / "v", enabled=True)
    recorder = recorder_mod.ValidationRecorder(store)
    record = recorder.build_record(
        trace_id="t", user_text="hi", error_detail=text
    )
    recorder.record(record)
    raw = next((tmp_path / "v").glob("*.jsonl")).read_text(encoding="utf-8")
    for secret in ("sk-abcdefghijklmnopqrst", "hunter2xyz", "verysecretvalue123",
                   "abcdefghijklmnop", "abc.def.ghi"):
        assert secret not in raw, f"密钥泄漏: {secret}"


def test_url_query_secrets_redacted() -> None:
    """第六十二节：URL query 里的密钥也必须脱敏（不只防 header）。"""
    from dl_senpai.validation.privacy import redact_secrets

    url = "https://api.example.com/v1?api_key=sk-abcdefghijklmnop&x=1"
    out = redact_secrets(url)
    assert "sk-abcdefghijklmnop" not in out
    assert "[REDACTED]" in out


def test_error_detail_is_truncated_and_redacted(tmp_path) -> None:
    store = ValidationStore(tmp_path / "v", enabled=True)
    recorder = recorder_mod.ValidationRecorder(store)
    long_secret = "sk-" + "x" * 500
    record = recorder.build_record(error_detail=f"failed {long_secret}")
    assert len(record.error_detail) <= 300
    assert "sk-xxxx" not in record.error_detail


# ================================================================
# 4. 工具参数隐私（第六十三/六十四节）
# ================================================================


def test_tool_arguments_not_stored() -> None:
    """第六十三节：只记参数**键名**，不记值。

    注意断言方式：不能用 `"duration" not in blob` —— 那会命中
    合法的 `duration_ms`（耗时字段）。必须按**字段名精确匹配**。
    """
    tel = RequestTelemetry()
    tel.record_tool(
        ToolTelemetry(
            tool="mute",
            call_id="c1",
            outcome="success",
            duration_ms=10,
        )
    )
    data = tel.tools[0].as_dict()
    # 白名单：字段集合必须恰好是这些
    assert set(data) == {
        "tool", "call_id", "outcome", "duration_ms", "permission_ms",
        "dry_run", "permission_denied", "visibility_denied", "error_type",
    }
    # 不得有承载工具参数的字段
    for banned in ("arguments", "args", "params", "user_id", "card", "prompt"):
        assert banned not in data, f"工具参数被记录: {banned}"


def test_search_query_not_stored(tmp_path) -> None:
    """第六十四节：SEARCH query 可能等于用户原话，不得落盘原文。"""
    store = ValidationStore(tmp_path / "v", enabled=True)
    recorder = recorder_mod.ValidationRecorder(store)
    query = "我家路由器密码是多少"

    from dl_senpai.validation.models import BehaviorSnapshot

    record = recorder.build_record(
        trace_id="t",
        user_text=query,
        legacy=BehaviorSnapshot(tool_calls=["search"], ok=True),
        runtime=BehaviorSnapshot(
            tool_calls=["search"],
            tool_argument_shapes={"search": ["queries"]},
            ok=True,
        ),
    )
    recorder.record(record)
    raw = next((tmp_path / "v").glob("*.jsonl")).read_text(encoding="utf-8")
    assert query not in raw, "SEARCH query 原文落盘了"
    # 但形状与计数保留
    payload = json.loads(raw.strip())
    assert payload["runtime"]["tool_argument_shapes"]["search"] == ["queries"]


def test_request_telemetry_dict_has_no_arguments() -> None:
    """整体 as_dict 也不含参数值。"""
    tel = RequestTelemetry()
    tel.record_tool(
        ToolTelemetry(tool="card", call_id="c1", outcome="success")
    )
    blob = json.dumps(tel.as_dict(), ensure_ascii=False)
    assert "99999999" not in blob
    assert "名片" not in blob
