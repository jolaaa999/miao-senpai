"""可观测性测试：trace 贯穿、结构化日志、token usage、指标、重试策略。"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from dl_senpai.observability import (
    metrics,
    trace_id,
    trace_scope,
    trace_stage,
)
from dl_senpai.observability.logging import format_fields, log_event
from dl_senpai.observability.retry import (
    DEFAULT_RETRY_POLICY,
    NoWaitRetryPolicy,
    RetryPolicy,
)


@pytest.fixture(autouse=True)
def _clean_metrics():
    metrics.reset()
    yield
    metrics.reset()


# ---------------------------------------------------------------- trace


def test_trace_id_empty_outside_scope() -> None:
    assert trace_id() == ""


def test_trace_scope_assigns_id() -> None:
    with trace_scope(session_id="group:1") as ctx:
        assert ctx.trace_id
        assert trace_id() == ctx.trace_id
    assert trace_id() == ""


def test_trace_id_stable_within_scope() -> None:
    with trace_scope(session_id="group:1"):
        first = trace_id()
        second = trace_id()
    assert first == second


def test_nested_scope_inherits_trace_id() -> None:
    """一次消息的多个阶段必须共用同一个 trace_id。"""
    with trace_scope(session_id="group:1") as outer:
        with trace_stage("llm.request"):
            inner = trace_id()
    assert inner == outer.trace_id


def test_nested_scope_overrides_stage() -> None:
    from dl_senpai.observability import current_trace

    with trace_scope(session_id="group:1", stage="chat"):
        assert current_trace().stage == "chat"
        with trace_stage("tool"):
            assert current_trace().stage == "tool"
        assert current_trace().stage == "chat"


def test_trace_stage_without_outer_scope_is_noop() -> None:
    with trace_stage("llm"):
        assert trace_id() == ""


def test_trace_scope_restores_after_exception() -> None:
    with pytest.raises(RuntimeError):
        with trace_scope(session_id="group:1"):
            raise RuntimeError("boom")
    assert trace_id() == ""


def test_trace_ids_differ_across_scopes() -> None:
    with trace_scope() as a:
        pass
    with trace_scope() as b:
        pass
    assert a.trace_id != b.trace_id


def test_trace_context_fields() -> None:
    with trace_scope(session_id="group:1", message_id="m9", user_id="7") as ctx:
        fields = ctx.as_log_fields()
    assert fields["session_id"] == "group:1"
    assert fields["message_id"] == "m9"
    assert fields["user_id"] == "7"


def test_trace_elapsed_ms() -> None:
    with trace_scope() as ctx:
        assert ctx.elapsed_ms() >= 0


@pytest.mark.asyncio
async def test_trace_is_task_isolated() -> None:
    """并发任务之间 trace 不串台（contextvars 的核心价值）。"""
    seen: dict[str, str] = {}

    async def worker(name: str) -> None:
        with trace_scope(session_id=name):
            await asyncio.sleep(0.01)
            seen[name] = trace_id()

    await asyncio.gather(worker("a"), worker("b"))
    assert seen["a"] != seen["b"]


# ---------------------------------------------------------------- 结构化日志


def test_format_fields_skips_empty() -> None:
    assert format_fields({"a": 1, "b": None, "c": ""}) == "a=1"


def test_format_fields_quotes_spaces() -> None:
    assert format_fields({"msg": "hello world"}) == 'msg="hello world"'


def test_format_fields_redacts_secrets() -> None:
    out = format_fields({"api_key": "sk-secret", "token": "t", "password": "p"})
    assert "sk-secret" not in out
    assert out.count("<redacted>") == 3


def test_format_fields_single_line() -> None:
    """换行必须被压成空格，否则会破坏 Go 控制台按行读取。"""
    out = format_fields({"text": "line1\nline2"})
    assert "\n" not in out


def test_format_fields_bool_and_float() -> None:
    out = format_fields({"ok": True, "ratio": 0.123456789})
    assert "ok=true" in out
    assert "ratio=0.1235" in out


def test_log_event_does_not_raise(capsys) -> None:
    log_event("test.event", session_id="group:1", value=1)  # 不应抛


def test_log_event_includes_trace_id() -> None:
    """结构化日志要能被 trace_id 串起来。"""
    captured: list[str] = []

    from dl_senpai.observability import logging as logging_mod

    class FakeLogger:
        def info(self, msg: str) -> None:
            captured.append(msg)

        def warning(self, msg: str) -> None:
            captured.append(msg)

    original = logging_mod._LOG
    logging_mod._LOG = FakeLogger()
    try:
        with trace_scope(session_id="group:1"):
            tid = trace_id()
            log_event("llm.request", model="m")
    finally:
        logging_mod._LOG = original

    assert len(captured) == 1
    assert tid in captured[0]
    assert "model=m" in captured[0]
    assert "session_id=group:1" in captured[0]


# ---------------------------------------------------------------- metrics


def test_metrics_counter() -> None:
    metrics.incr("llm.request")
    metrics.incr("llm.request")
    assert metrics.counter("llm.request") == 2.0


def test_metrics_counter_default_zero() -> None:
    assert metrics.counter("nothing") == 0.0


def test_metrics_histogram_percentiles() -> None:
    """分位数用 nearest-rank 语义（不插值）。

    样本 1..100，索引 = round((n-1)*q)：
    - p50 → round(99*0.5)=50 → 第 51 个样本 = 51
    - p99 → round(99*0.99)=98 → 第 99 个样本 = 99
    """
    for v in range(1, 101):
        metrics.observe("latency", float(v))
    hist = metrics.histogram("latency")
    assert hist["count"] == 100
    assert hist["avg"] == 50.5
    assert hist["p50"] == 51.0
    assert hist["p99"] == 99.0
    # 单调性：p50 <= p95 <= p99
    assert hist["p50"] <= hist["p95"] <= hist["p99"]


def test_metrics_histogram_single_sample() -> None:
    metrics.observe("one", 42.0)
    hist = metrics.histogram("one")
    assert hist["p50"] == hist["p95"] == hist["p99"] == 42.0


def test_metrics_histogram_bounded_samples() -> None:
    """长跑进程下延迟样本必须有界，不能无限增长。"""
    for i in range(5000):
        metrics.observe("many", float(i))
    hist = metrics.histogram("many")
    # count 记录真实次数，但内部只保留有限样本用于分位数
    assert hist["count"] == 5000


def test_metrics_histogram_empty() -> None:
    hist = metrics.histogram("none")
    assert hist["count"] == 0
    assert hist["p99"] is None


def test_metrics_gauge() -> None:
    metrics.gauge("queue.length", 5)
    assert metrics.read_gauge("queue.length") == 5


def test_metrics_snapshot() -> None:
    metrics.incr("a")
    metrics.gauge("b", 1)
    metrics.observe("c", 1.0)
    snap = metrics.snapshot()
    assert snap["counters"]["a"] == 1
    assert snap["gauges"]["b"] == 1
    assert snap["histograms"]["c"]["count"] == 1


def test_metrics_reset() -> None:
    metrics.incr("a")
    metrics.reset()
    assert metrics.counter("a") == 0.0


# ---------------------------------------------------------------- 重试策略


def test_default_policy_matches_legacy_behavior() -> None:
    """生产默认：4 次尝试；瞬时错误改为指数退避（原先固定 60 秒）。"""
    assert DEFAULT_RETRY_POLICY.max_attempts == 4
    assert DEFAULT_RETRY_POLICY.transient_base_delay == 2.0
    assert DEFAULT_RETRY_POLICY.transient_max_delay == 10.0


def test_transient_exponential_backoff() -> None:
    """2s/4s/8s 之后封顶 10s——最坏总等待要从 180s 降到约 14s。"""
    p = DEFAULT_RETRY_POLICY
    assert p.delay_for_transient(0) == 2.0
    assert p.delay_for_transient(1) == 4.0
    assert p.delay_for_transient(2) == 8.0
    assert p.delay_for_transient(9) == 10.0  # 封顶
    total = sum(p.delay_for_transient(i) for i in range(p.max_attempts - 1))
    assert total <= 15.0


def test_transient_retry_budget() -> None:
    assert DEFAULT_RETRY_POLICY.should_retry_transient(0) is True
    assert DEFAULT_RETRY_POLICY.should_retry_transient(2) is True
    assert DEFAULT_RETRY_POLICY.should_retry_transient(3) is False


def test_unexpected_exponential_backoff() -> None:
    p = DEFAULT_RETRY_POLICY
    assert p.delay_for_unexpected(0) == 0.5
    assert p.delay_for_unexpected(1) == 1.0
    assert p.delay_for_unexpected(2) == 2.0
    assert p.delay_for_unexpected(9) == 4.0  # 封顶


def test_no_wait_policy_has_zero_delay() -> None:
    p = NoWaitRetryPolicy()
    assert p.delay_for_transient(0) == 0.0
    assert p.delay_for_unexpected(3) == 0.0


def test_no_wait_policy_keeps_retry_count() -> None:
    """零等待不能改变"重试到第几次"的语义，否则行为没被真正覆盖。"""
    p = NoWaitRetryPolicy(max_attempts=4)
    assert p.should_retry_transient(0) is True
    assert p.should_retry_transient(3) is False


def test_policy_can_disable_retry() -> None:
    p = RetryPolicy(retry_transient=False)
    assert p.should_retry_transient(0) is False


@pytest.mark.asyncio
async def test_policy_sleep_can_be_noop() -> None:
    await NoWaitRetryPolicy().sleep(60.0)  # 必须立即返回


@pytest.mark.asyncio
async def test_policy_sleep_zero_skips() -> None:
    await RetryPolicy(transient_base_delay=0.0).sleep(0.0)


# ---------------------------------------------------------------- LLM usage


class _FakeUsage:
    def __init__(self, p: int | None, c: int | None, t: int | None) -> None:
        self.prompt_tokens = p
        self.completion_tokens = c
        self.total_tokens = t


def _llm_with_usage(usage: Any):
    from dl_senpai.config import PluginConfig
    from dl_senpai.llm import SenpaiLLM

    cfg = PluginConfig(
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="test-model",
    )
    llm = SenpaiLLM(cfg, retry=NoWaitRetryPolicy())

    msg = MagicMock()
    msg.content = "学姐来啦～"
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage

    client = MagicMock()
    client.chat.completions.create = AsyncMock(return_value=resp)
    llm.use_client(client)
    return llm


@pytest.mark.asyncio
async def test_usage_recorded_when_present() -> None:
    llm = _llm_with_usage(_FakeUsage(10, 5, 15))
    await llm.chat([], "你好", sender_name="小明")
    assert metrics.counter("llm.tokens.total") == 15.0
    assert metrics.counter("llm.tokens.prompt") == 10.0
    assert metrics.counter("llm.tokens.completion") == 5.0
    assert metrics.counter("llm.request") == 1.0


@pytest.mark.asyncio
async def test_usage_missing_does_not_fail() -> None:
    """中转站不返回 usage 时，请求必须照常成功（禁止因统计失败而报错）。"""
    llm = _llm_with_usage(None)
    result = await llm.chat([], "你好", sender_name="小明")
    assert "学姐" in result.text
    assert metrics.counter("llm.request") == 1.0
    assert metrics.counter("llm.tokens.total") == 0.0


@pytest.mark.asyncio
async def test_usage_dict_form_supported() -> None:
    """部分兼容实现用 dict 而非对象返回 usage。"""

    class _Resp:
        def __init__(self) -> None:
            self.usage = {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}

    from dl_senpai.config import PluginConfig
    from dl_senpai.llm import SenpaiLLM

    cfg = PluginConfig(
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="test-model",
    )
    llm = SenpaiLLM(cfg, retry=NoWaitRetryPolicy())
    llm._record_usage(_Resp(), model="m", duration_ms=1.0)
    assert metrics.counter("llm.tokens.total") == 7.0


@pytest.mark.asyncio
async def test_llm_latency_observed() -> None:
    llm = _llm_with_usage(_FakeUsage(1, 1, 2))
    await llm.chat([], "你好", sender_name="小明")
    assert metrics.histogram("llm.latency_ms")["count"] == 1


@pytest.mark.asyncio
async def test_injected_client_is_used_not_real_one() -> None:
    """注入的客户端必须被真正使用（防止偷偷新建真实客户端联网）。"""
    llm = _llm_with_usage(_FakeUsage(1, 1, 2))
    client = llm._injected_client
    await llm.chat([], "你好", sender_name="小明")
    assert client.chat.completions.create.await_count == 1
