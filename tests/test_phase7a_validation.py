"""Phase 7A：Validation 基础设施测试。

覆盖第四十节要求：
    anonymization stability / no plaintext ids / no secrets
    no full prompt / no full user text
    shadow no side effect
    mismatch classification / severity classification
    metrics aggregation / latency stages / token aggregation / queue metrics
    retention cleanup

测试全部用 Fake，**不要求真实群**（第四十一节）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from dl_senpai.validation import (
    BehaviorSnapshot,
    CostTracker,
    ErrorClass,
    MismatchType,
    Severity,
    ValidationRecorder,
    ValidationStore,
    anonymize,
    check_scope,
    compare,
    contains_secret,
    content_hash,
    count_by_severity,
    count_by_type,
    error_log_level,
    has_blocking_mismatch,
    memory_metrics,
    message_profile,
    percentile,
    prompt_profile,
    redact_secrets,
    severity_for,
    summarize,
    summarize_latency,
    summarize_memory,
    summarize_queue,
    summarize_send,
    summarize_tools,
)


# ================================================================
# 1. 匿名化（第八节）
# ================================================================


def test_anonymize_is_stable() -> None:
    """同一 id 永远得到同一匿名 id（否则无法统计同一用户）。"""
    a = anonymize("123456", kind="user")
    b = anonymize("123456", kind="user")
    assert a == b
    assert a.startswith("u_")


def test_anonymize_differs_by_kind() -> None:
    """不同类型 id 不撞名（同一个数字既是群号又是用户号时）。"""
    assert anonymize("123456", kind="user") != anonymize("123456", kind="group")
    assert anonymize("123456", kind="group") != anonymize("123456", kind="session")


def test_anonymize_does_not_contain_plaintext() -> None:
    """匿名 id 里绝不能出现原始 QQ 号。

    注意：不能只做 `real not in out` 的子串检查 —— 短 id（如 `999`）
    会偶然出现在哈希十六进制里。真正要断言的是：
        匿名 id **不是**由真实 id 直接拼接/可逆变换而来，
        且不同真实 id 产生不同匿名 id（无碰撞风险）。
    """
    real = "1234567890"
    out = anonymize(real, kind="user")
    assert out != real
    # 不能是把真实 id 直接嵌进来
    assert not out.endswith(real)
    assert not out.startswith(real)
    assert out.isascii()
    # 结构：prefix + 16 位十六进制
    prefix, _, digest = out.partition("_")
    assert prefix == "u"
    assert len(digest) == 16
    int(digest, 16)  # 必须是合法十六进制
    # 不同 id → 不同匿名 id
    assert out != anonymize("1234567891", kind="user")


def test_anonymize_is_not_trivially_reversible() -> None:
    """匿名 id 不得等于真实 id 的裸哈希（必须加盐）。"""
    import hashlib

    real = "1234567890"
    plain = hashlib.sha256(real.encode()).hexdigest()[:16]
    assert anonymize(real, kind="user") != f"u_{plain}"


def test_anonymize_empty() -> None:
    assert anonymize("", kind="user") == ""
    assert anonymize(None, kind="user") == ""


def test_anonymize_salt_changes_output(monkeypatch) -> None:
    """换盐后匿名 id 变化（证明用的是 HMAC 而非裸哈希）。"""
    first = anonymize("123456", kind="user")
    monkeypatch.setenv("DL_SENPAI_VALIDATION_SALT", "another-salt-value")
    second = anonymize("123456", kind="user")
    assert first != second


def test_content_hash_is_stable_and_salted(monkeypatch) -> None:
    assert content_hash("你好") == content_hash("你好")
    monkeypatch.setenv("DL_SENPAI_VALIDATION_SALT", "x")
    assert content_hash("你好") != ""


def test_content_hash_short_text_not_reversible() -> None:
    """短文本的哈希不能直接等于常见摘要（加盐的意义）。"""
    import hashlib

    plain = hashlib.sha256("在吗".encode()).hexdigest()[:16]
    assert content_hash("在吗") != plain


# ================================================================
# 2. 内容策略（第九/十节）
# ================================================================


def test_message_profile_has_no_text() -> None:
    """消息画像不得包含原文。"""
    text = "我的手机号是13800138000"
    profile = message_profile(text, message_type="group", has_image=True)
    assert text not in json.dumps(profile, ensure_ascii=False)
    assert "13800138000" not in json.dumps(profile, ensure_ascii=False)
    assert profile["length"] == len(text)
    assert profile["has_image"] is True
    assert profile["hash"]


def test_message_profile_fields() -> None:
    p = message_profile("hi", message_type="private", has_reply=True, is_to_me=True)
    assert set(p) == {
        "length", "message_type", "has_image", "has_reply",
        "is_to_me", "command_match", "hash",
    }
    assert p["message_type"] == "private"
    assert p["has_reply"] is True
    assert p["is_to_me"] is True


def test_prompt_profile_has_no_content() -> None:
    """Prompt 画像不得包含 persona / history / 用户消息。"""
    system = "你是学姐，一只猫娘。"
    messages = [{"role": "user", "content": "我的密码是abc12345"}]
    profile = prompt_profile(system_prompt=system, messages=messages, tool_count=8)
    blob = json.dumps(profile, ensure_ascii=False)
    assert "学姐" not in blob
    assert "abc12345" not in blob
    assert profile["section_count"] == 2  # system + 1 message
    assert profile["tool_count"] == 8
    assert profile["prompt_hash"]
    assert profile["prompt_length"] > 0


def test_debug_content_defaults_off(monkeypatch) -> None:
    monkeypatch.delenv("DL_SENPAI_DEBUG_CONTENT", raising=False)
    from dl_senpai.validation import debug_content_enabled

    assert debug_content_enabled() is False


# ================================================================
# 3. 密钥防护（第三十九节）
# ================================================================


@pytest.mark.parametrize(
    "text",
    [
        "sk-abcdefghijklmnopqrst",
        "Authorization: Bearer abc1234567890",
        "api_key=supersecretvalue",
        "password: hunter2xyz",
        "token=abcdefghijklmnop",
    ],
)
def test_contains_secret_detects(text: str) -> None:
    assert contains_secret(text) is True


def test_contains_secret_negative() -> None:
    assert contains_secret("今天天气不错") is False
    assert contains_secret("模型调用失败") is False


def test_redact_secrets_replaces() -> None:
    out = redact_secrets("key=sk-abcdefghijklmnop")
    assert "sk-abcdefghijklmnop" not in out
    assert "[REDACTED]" in out


def test_recorder_redacts_error_detail() -> None:
    """错误详情也可能含密钥（URL 里带 key），必须脱敏。"""
    rec = ValidationRecorder()
    record = rec.build_record(
        error_detail="failed to call https://api.x.com?api_key=sk-abcdefghijklmnop"
    )
    assert "sk-abcdefghijklmnop" not in record.error_detail


# ================================================================
# 4. 比较器（第十一/十二/十三节）
# ================================================================


def snap(**kw: Any) -> BehaviorSnapshot:
    return BehaviorSnapshot(**kw)


def test_identical_snapshots_produce_no_mismatch() -> None:
    a = snap(tool_calls=["search"], visible_tools=["search"], stop_reason="finished",
             model_call_count=1, total_tokens=100, ok=True)
    b = snap(tool_calls=["search"], visible_tools=["search"], stop_reason="finished",
             model_call_count=1, total_tokens=100, ok=True)
    assert compare(a, b) == []


def test_trigger_mismatch_is_high() -> None:
    a = snap(triggered=True, ok=True)
    b = snap(triggered=False, ok=True)
    ms = compare(a, b)
    assert any(m.type is MismatchType.TRIGGER_MISMATCH for m in ms)
    sev = [m.severity for m in ms if m.type is MismatchType.TRIGGER_MISMATCH][0]
    assert sev is Severity.HIGH


def test_permission_mismatch_is_critical() -> None:
    """权限差异必须 CRITICAL —— 一边禁言一边不禁言是用户可见差异。"""
    a = snap(permission_denied=["mute"], tool_calls=["mute"], ok=True)
    b = snap(permission_denied=[], tool_calls=[], ok=True)
    ms = compare(a, b)
    assert any(
        m.type is MismatchType.PERMISSION_MISMATCH and m.severity is Severity.CRITICAL
        for m in ms
    )


def test_dangerous_tool_selection_is_critical() -> None:
    """涉及 mute/card 的工具选择差异升级为 CRITICAL。"""
    a = snap(tool_calls=["mute"], ok=True)
    b = snap(tool_calls=["search"], ok=True)
    ms = compare(a, b)
    sel = [m for m in ms if m.type is MismatchType.TOOL_SELECTION_MISMATCH]
    assert sel and sel[0].severity is Severity.CRITICAL


def test_non_dangerous_tool_selection_is_high() -> None:
    a = snap(tool_calls=["search"], ok=True)
    b = snap(tool_calls=["draw"], ok=True)
    ms = compare(a, b)
    sel = [m for m in ms if m.type is MismatchType.TOOL_SELECTION_MISMATCH]
    assert sel and sel[0].severity is Severity.HIGH


def test_visible_tool_mismatch_is_low() -> None:
    a = snap(visible_tools=["search", "draw"], ok=True)
    b = snap(visible_tools=["search"], ok=True)
    ms = compare(a, b)
    vis = [m for m in ms if m.type is MismatchType.VISIBLE_TOOL_MISMATCH]
    assert vis and vis[0].severity is Severity.LOW


def test_recovery_mismatch_is_medium() -> None:
    a = snap(recovery_triggered=["card"], ok=True)
    b = snap(recovery_triggered=[], ok=True)
    ms = compare(a, b)
    rec = [m for m in ms if m.type is MismatchType.RECOVERY_MISMATCH]
    assert rec and rec[0].severity is Severity.MEDIUM


def test_stop_reason_guard_difference_is_high() -> None:
    """一方正常完成、另一方被护栏停止 → HIGH。"""
    a = snap(stop_reason="finished", ok=True)
    b = snap(stop_reason="max_steps", ok=True)
    ms = compare(a, b)
    sr = [m for m in ms if m.type is MismatchType.STOP_REASON_MISMATCH]
    assert sr and sr[0].severity is Severity.HIGH


def test_stop_reason_normal_difference_is_low() -> None:
    a = snap(stop_reason="finished", ok=True)
    b = snap(stop_reason="no_tool_call", ok=True)
    ms = compare(a, b)
    sr = [m for m in ms if m.type is MismatchType.STOP_REASON_MISMATCH]
    assert sr and sr[0].severity is Severity.LOW


def test_argument_shape_mismatch_detected() -> None:
    """参数键形状不同（不看值）。"""
    a = snap(tool_calls=["card"], tool_argument_shapes={"card": ["user_id", "card"]}, ok=True)
    b = snap(tool_calls=["card"], tool_argument_shapes={"card": ["user_id"]}, ok=True)
    ms = compare(a, b)
    assert any(m.type is MismatchType.TOOL_ARGUMENT_MISMATCH for m in ms)


def test_token_difference_within_noise_ignored() -> None:
    """token 小幅波动属噪声，不应报差异。"""
    a = snap(total_tokens=1000, ok=True)
    b = snap(total_tokens=1050, ok=True)
    ms = compare(a, b)
    assert not any("token" in m.detail for m in ms)


def test_token_difference_beyond_noise_reported() -> None:
    a = snap(total_tokens=1000, ok=True)
    b = snap(total_tokens=3000, ok=True)
    ms = compare(a, b)
    assert any("token" in m.detail for m in ms)


def test_model_call_count_mismatch() -> None:
    a = snap(model_call_count=1, ok=True)
    b = snap(model_call_count=3, ok=True)
    ms = compare(a, b)
    assert any(m.type is MismatchType.MODEL_CALL_COUNT_MISMATCH for m in ms)


def test_expected_difference_is_info() -> None:
    """已知合理差异降级为 INFO/EXPECTED_DIFFERENCE。"""
    a = snap(visible_tools=["search"], ok=True)
    b = snap(visible_tools=["search", "knowledge"], ok=True)
    ms = compare(a, b, expected_differences={MismatchType.VISIBLE_TOOL_MISMATCH})
    assert ms and all(m.severity is Severity.INFO for m in ms)
    assert all(m.type is MismatchType.EXPECTED_DIFFERENCE for m in ms)


def test_missing_side_reported() -> None:
    ms = compare(None, snap(ok=True))
    assert ms and any("未产生快照" in m.detail for m in ms)


def test_count_helpers() -> None:
    a = snap(tool_calls=["mute"], permission_denied=["mute"], ok=True)
    b = snap(tool_calls=[], ok=True)
    ms = compare(a, b)
    by_sev = count_by_severity(ms)
    by_type = count_by_type(ms)
    assert by_sev["critical"] >= 1
    assert sum(by_type.values()) == len(ms)
    assert has_blocking_mismatch(ms) is True


def test_no_match_rate_metric_exists() -> None:
    """第十四节：**不得**提供"总匹配率"这类指标。"""
    import dl_senpai.validation as v

    for banned in ("match_rate", "similarity", "agreement_rate", "consistency"):
        assert not hasattr(v, banned), f"不应提供 {banned} 这类聚合指标"


# ================================================================
# 5. 严重度映射（第十五节）
# ================================================================


def test_severity_escalates_for_dangerous_tools() -> None:
    assert severity_for(
        MismatchType.TOOL_ARGUMENT_MISMATCH, tools={"mute"}
    ) is Severity.CRITICAL
    assert severity_for(
        MismatchType.TOOL_ARGUMENT_MISMATCH, tools={"search"}
    ) is Severity.MEDIUM


def test_severity_cross_group_is_critical() -> None:
    assert severity_for(
        MismatchType.EXPECTED_DIFFERENCE, cross_group=True
    ) is Severity.CRITICAL


# ================================================================
# 6. 错误分级（第三十/三十一节）
# ================================================================


def test_permission_denied_is_info() -> None:
    """权限拒绝是正常业务事件，不应打 ERROR。"""
    assert error_log_level(ErrorClass.PERMISSION_DENIED) == "info"


def test_config_error_is_error() -> None:
    assert error_log_level(ErrorClass.CONFIG_ERROR) == "error"


def test_timeout_is_warning() -> None:
    assert error_log_level(ErrorClass.TOOL_TIMEOUT) == "warning"


def test_unknown_error_defaults_warning() -> None:
    assert error_log_level("something_else") == "warning"


# ================================================================
# 7. 存储与保留期（第三十四/三十七节）
# ================================================================


def test_store_disabled_by_default(tmp_path, monkeypatch) -> None:
    """默认不落盘（避免开发机产生垃圾）。

    注意：不能用 `monkeypatch.delenv` —— 删掉环境变量会让
    `config_env.get_env` 回退到开发机的 `.env`（可能有 VALIDATION=1）。
    必须用哨兵值表达"显式未配置"。
    """
    from dl_senpai.config_env import UNSET_SENTINEL

    monkeypatch.setenv("DL_SENPAI_VALIDATION", UNSET_SENTINEL)
    store = ValidationStore(tmp_path / "v")
    rec = ValidationRecorder(store)
    record = rec.build_record(user_text="hi")
    assert store.append(record) is False
    assert not (tmp_path / "v").exists() or not list((tmp_path / "v").glob("*.jsonl"))


def test_store_writes_jsonl(tmp_path) -> None:
    """落盘内容必须是脱敏后的结构数据。

    **不能**用 `"999" not in raw` 这种子串断言 ——
    短数字会偶然出现在哈希十六进制或 length 里。
    正确做法是解析 JSON 后逐个字段验证：
    真正承载身份的字段（user_hash / message.hash / message.length）
    不含明文 id。
    """
    store = ValidationStore(tmp_path / "v", enabled=True)
    rec = ValidationRecorder(store)
    record = rec.build_record(
        trace_id="t1", user_text="你好", user_id="999", group_id="888"
    )
    assert store.append(record) is True
    files = list((tmp_path / "v").glob("*.jsonl"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert payload["trace_id"] == "t1"

    # 身份字段必须是匿名 hash，而不是明文
    assert payload["user_hash"] != "999"
    assert payload["group_hash"] != "888"
    assert payload["user_hash"].startswith("u_")
    assert payload["group_hash"].startswith("g_")
    # 消息只有结构画像，没有原文
    assert "text" not in payload["message"]
    assert "你好" not in json.dumps(payload["message"], ensure_ascii=False)
    assert payload["message"]["length"] == 2


def test_store_contains_no_plaintext_identity(tmp_path) -> None:
    """用长 id 做严格断言：明文 id 不得出现在落盘内容里。

    短数字不可靠（会命中哈希子串），因此这里用真实长度的 QQ 号形态。
    """
    store = ValidationStore(tmp_path / "v", enabled=True)
    rec = ValidationRecorder(store)
    user_id = "1234567890"
    group_id = "987654321"
    rec.record(rec.build_record(
        trace_id="t1",
        user_id=user_id,
        group_id=group_id,
        session_id=f"group:{group_id}",
        user_text="随便说点什么",
    ))
    raw = next((tmp_path / "v").glob("*.jsonl")).read_text(encoding="utf-8")
    assert user_id not in raw
    assert group_id not in raw
    assert "随便说点什么" not in raw


def test_store_read_filters_by_time(tmp_path) -> None:
    store = ValidationStore(tmp_path / "v", enabled=True)
    rec = ValidationRecorder(store)
    old = rec.build_record(trace_id="old", user_text="a")
    old.timestamp = time.time() - 86400 * 3
    new = rec.build_record(trace_id="new", user_text="b")
    store.append(old)
    store.append(new)

    recent = store.read(since=time.time() - 3600)
    assert [r["trace_id"] for r in recent] == ["new"]


def test_store_cleanup_retention(tmp_path) -> None:
    """保留期清理（第三十七节）。"""
    store = ValidationStore(tmp_path / "v", enabled=True, retention_days=7)
    rec = ValidationRecorder(store)
    store.append(rec.build_record(trace_id="x", user_text="hi"))
    files = list((tmp_path / "v").glob("*.jsonl"))
    assert files

    # 把文件改成 8 天前
    old_time = time.time() - 86400 * 8
    os.utime(files[0], (old_time, old_time))
    removed = store.cleanup()
    assert removed
    assert not list((tmp_path / "v").glob("*.jsonl"))


def test_store_cleanup_zero_days_keeps_all(tmp_path) -> None:
    """retention_days=0 表示不清理。"""
    store = ValidationStore(tmp_path / "v", enabled=True, retention_days=0)
    rec = ValidationRecorder(store)
    store.append(rec.build_record(trace_id="x", user_text="hi"))
    files = list((tmp_path / "v").glob("*.jsonl"))
    old_time = time.time() - 86400 * 999
    os.utime(files[0], (old_time, old_time))
    assert store.cleanup() == []


def test_store_skips_corrupt_lines(tmp_path) -> None:
    """半行写入不应导致整体读取失败。"""
    store = ValidationStore(tmp_path / "v", enabled=True)
    rec = ValidationRecorder(store)
    store.append(rec.build_record(trace_id="ok", user_text="hi"))
    files = list((tmp_path / "v").glob("*.jsonl"))
    with open(files[0], "a", encoding="utf-8") as fh:
        fh.write('{"broken": \n')
    rows = store.read()
    assert len(rows) == 1
    assert rows[0]["trace_id"] == "ok"


def test_store_stats(tmp_path) -> None:
    store = ValidationStore(tmp_path / "v", enabled=True, retention_days=14)
    rec = ValidationRecorder(store)
    store.append(rec.build_record(trace_id="x", user_text="hi"))
    stats = store.stats()
    assert stats["enabled"] is True
    assert stats["files"] == 1
    assert stats["retention_days"] == 14


def test_recorder_never_raises(tmp_path) -> None:
    """观测失败绝不影响业务。"""
    class Broken:
        def append(self, record):
            raise RuntimeError("disk full")

    rec = ValidationRecorder(Broken())
    record = rec.build_record(user_text="hi")
    assert rec.record(record) is False
    assert rec.counters["write_failed"] == 1


# ================================================================
# 8. 指标聚合（第十六/十九/二十三节）
# ================================================================


def test_percentile_linear_interpolation() -> None:
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(values, 50) == pytest.approx(5.5)
    assert percentile(values, 100) == 10
    assert percentile([], 50) == 0.0
    assert percentile([7], 95) == 7


def test_latency_summary_has_p50_p95_p99() -> None:
    s = summarize_latency([1.0, 2.0, 3.0, 4.0, 5.0])
    assert set(s) >= {"count", "p50", "p95", "p99", "max"}
    assert s["p50"] <= s["p95"] <= s["p99"] <= s["max"]


def _row(**kw: Any) -> dict[str, Any]:
    base = {
        "group_hash": "g_1",
        "trigger_reason": "mention",
        "latency": {},
        "mismatches": [],
        "legacy": {"ok": True},
        "runtime": {"ok": True, "tool_calls": [], "model_call_count": 1,
                    "total_tokens": 100, "prompt_tokens": 60,
                    "completion_tokens": 40},
        "prompt": {"model": "m1"},
    }
    base.update(kw)
    return base


def test_summarize_counts_requests_and_success() -> None:
    rows = [_row(), _row(runtime={"ok": False, "tool_calls": []})]
    out = summarize(rows)
    assert out["requests"] == 2
    assert out["runtime_success_rate"] == 50.0
    assert out["legacy_success_rate"] == 100.0


def test_summarize_mismatch_distribution() -> None:
    rows = [
        _row(mismatches=[
            {"type": "permission_mismatch", "severity": "critical"},
            {"type": "trigger_mismatch", "severity": "high"},
        ])
    ]
    out = summarize(rows)
    assert out["mismatch_by_type"]["permission_mismatch"] == 1
    assert out["mismatch_by_severity"]["critical"] == 1
    assert out["mismatch_by_severity"]["high"] == 1


def test_summarize_latency_stages_split() -> None:
    """第十九节：延迟必须分阶段。"""
    rows = [
        _row(latency={"total": 5.0, "queue_wait": 1.0, "model": 3.0, "tool": 1.0}),
        _row(latency={"total": 7.0, "queue_wait": 2.0, "model": 4.0, "tool": 1.0}),
    ]
    out = summarize(rows)
    assert out["latency"]["count"] == 2
    assert "queue_wait" in out["latency_stages"]
    assert "model" in out["latency_stages"]
    assert out["latency_stages"]["model"]["p50"] == pytest.approx(3.5)


def test_summarize_tokens() -> None:
    rows = [_row(), _row()]
    out = summarize(rows)
    assert out["tokens"]["total"] == 200
    assert out["tokens"]["prompt"] == 120
    assert out["tokens"]["completion"] == 80
    assert out["tokens"]["per_request"] == 100.0


def test_summarize_tool_calls() -> None:
    rows = [
        _row(runtime={"ok": True, "tool_calls": ["search", "search"]}),
        _row(runtime={"ok": True, "tool_calls": ["draw"]}),
    ]
    out = summarize(rows)
    assert out["tool_calls"]["search"] == 2
    assert out["tool_calls"]["draw"] == 1


def test_summarize_by_group_is_anonymized() -> None:
    out = summarize([_row()])
    assert "g_1" in out["by_group"]


def test_summarize_empty() -> None:
    out = summarize([])
    assert out["requests"] == 0
    assert out["runtime_success_rate"] == 0.0


def test_queue_metrics() -> None:
    rows = [
        _row(latency={"queue_wait": 0.5, "queue_depth": 3}),
        _row(latency={"queue_wait": 1.5, "queue_depth": 1}),
        _row(error_class="queue_rejected"),
        _row(session_hash="s_1"),
    ]
    out = summarize_queue(rows)
    assert out["queue_wait"]["count"] == 2
    assert out["queue_depth"]["max"] == 3
    assert out["rejected"] == 1
    assert out["active_sessions"] == 1


def test_tool_health_aggregation() -> None:
    rows = [
        _row(runtime={"ok": True, "tool_calls": ["search"]}),
        _row(runtime={"ok": False, "tool_calls": ["draw"],
                      "error_class": "tool_timeout"}),
    ]
    out = summarize_tools(rows)
    assert out["search"]["calls"] >= 1
    assert out["draw"]["timeout"] >= 1
    assert out["draw"]["failure"] >= 1


def test_dangerous_tool_denials_tracked() -> None:
    """第二十二节：mute/card 的拒绝要单独可见。"""
    rows = [
        _row(runtime={"ok": True, "tool_calls": ["mute"],
                      "permission_denied": ["mute"]}),
    ]
    out = summarize_tools(rows)
    assert out["mute"]["execution_denied"] >= 1


def test_memory_metrics_no_content() -> None:
    rows = [
        _row(memory={"history_count": 6, "recalled_count": 2,
                     "person_profile_hit": True, "group_style_hit": False,
                     "affection_loaded": True, "inner_state_loaded": True}),
    ]
    out = summarize_memory(rows)
    assert out["person_profile_hit"] == 1
    assert out["affection_loaded"] == 1
    assert out["history_count"]["count"] == 1
    # 不得出现内容字段
    blob = json.dumps(out)
    for banned in ("content", "text", "message", "profile_name"):
        assert banned not in blob


def test_send_metrics() -> None:
    rows = [
        _row(send={"send_success": 1, "send_failure": 0}),
        _row(send={"send_success": 0, "send_failure": 1, "retry": 2,
                   "media_send_failure": 1, "fallback_text": 1}),
    ]
    out = summarize_send(rows)
    assert out["send_success"] == 1
    assert out["send_failure"] == 1
    assert out["retry"] == 2
    assert out["media_send_failure"] == 1
    assert out["success_rate"] == 50.0


# ================================================================
# 9. Memory 作用域守卫（第二十四/二十五节）
# ================================================================


def test_scope_check_same_group_ok() -> None:
    r = check_scope("group:100", "group:100")
    assert r.ok is True


def test_scope_check_cross_group_is_critical() -> None:
    """群 A 请求不得拿到群 B 的数据。"""
    r = check_scope("group:100", "group:200")
    assert r.ok is False
    assert r.severity == "critical"
    assert r.reason == "cross_group_scope_violation"


def test_scope_check_user_profile_allowed_across_groups() -> None:
    """人物画像本来就是跨群认人，不算违规（第二十四节）。"""
    assert check_scope("group:100", "user:999").ok is True
    assert check_scope("group:100", "global").ok is True


def test_scope_check_group_data_in_non_group_request() -> None:
    r = check_scope("user:999", "group:100")
    assert r.ok is False
    assert r.severity == "medium"


def test_memory_metrics_marks_violation() -> None:
    bad = check_scope("group:100", "group:200")
    m = memory_metrics(history_count=3, scope_check=bad)
    assert m["scope_violation"] is True
    assert m["scope_reason"] == "cross_group_scope_violation"


def test_memory_metrics_fields_only() -> None:
    m = memory_metrics(history_count=4, recalled_count=1, person_profile_hit=True)
    assert set(m) == {
        "history_count", "recalled_count", "person_profile_hit",
        "group_style_hit", "affection_loaded", "inner_state_loaded",
        "scope_violation", "scope_reason",
    }


# ================================================================
# 10. 成本核算（第二十六~二十九节）
# ================================================================


def test_cost_unknown_when_no_pricing() -> None:
    """第二十七节：未配置价格 → unknown，**不是 0**。"""
    t = CostTracker()
    t.add(prompt_tokens=1000, completion_tokens=500)
    assert t.pricing_known is False
    assert t.cost() is None
    assert t.summary()["cost"] == "unknown"


def test_cost_computed_when_pricing_configured() -> None:
    t = CostTracker(input_price_per_million=1.0, output_price_per_million=2.0)
    t.add(prompt_tokens=1_000_000, completion_tokens=1_000_000)
    assert t.cost() == pytest.approx(3.0)


def test_cost_dimensions() -> None:
    t = CostTracker()
    t.add(prompt_tokens=100, completion_tokens=100,
          user_hash="u_1", group_hash="g_1", model="m1")
    t.add(prompt_tokens=50, completion_tokens=50,
          user_hash="u_1", group_hash="g_2", model="m2")
    s = t.summary()
    assert s["total_tokens"] == 300
    # 字段名是 top_users / top_groups（按用量排序的 Top N），
    # 不是原始 by_user/by_group —— 后者只在 tracker 内部保留
    assert s["top_users"]["u_1"] == 300
    assert set(s["top_groups"]) == {"g_1", "g_2"}
    assert set(s["by_model"]) == {"m1", "m2"}
    assert len(s["by_day"]) == 1


def test_cost_does_not_hardcode_price() -> None:
    """不得写死网络价格 —— 源码里不应出现具体金额常量。"""
    import inspect

    from dl_senpai.validation import cost as cost_mod

    src = inspect.getsource(cost_mod)
    # 允许 1_000_000（换算用），不允许出现形如 0.xx 的单价常量
    for banned in ("0.15", "0.60", "0.075", "0.30", "0.5, 1.5"):
        assert banned not in src, f"疑似写死了价格: {banned}"


def test_no_quota_enforcement() -> None:
    """第二十九节：本阶段只 observe，不强制 quota。"""
    t = CostTracker()
    for _ in range(100):
        t.add(prompt_tokens=1000, completion_tokens=1000)
    # 无论用多少都没有"拒绝"行为
    assert not hasattr(t, "blocked")
    assert not hasattr(t, "exceeded")


# ================================================================
# 11. 记录器端到端
# ================================================================


def test_recorder_end_to_end(tmp_path) -> None:
    store = ValidationStore(tmp_path / "v", enabled=True)
    rec = ValidationRecorder(store)

    record = rec.build_record(
        trace_id="tr-1",
        session_id="group:12345",
        user_id="67890",
        group_id="12345",
        execution_mode="shadow",
        trigger_reason="mention",
        user_text="学姐帮我看看这个",
        system_prompt="你是学姐",
        prompt_messages=[{"role": "user", "content": "学姐帮我看看这个"}],
        tool_count=8,
        legacy=BehaviorSnapshot(tool_calls=["search"], stop_reason="finished",
                                model_call_count=1, total_tokens=100, ok=True),
        runtime=BehaviorSnapshot(tool_calls=["search"], stop_reason="finished",
                                 model_call_count=1, total_tokens=100, ok=True),
        latency={"total": 2.0, "queue_wait": 0.1, "model": 1.5},
        memory={"history_count": 6},
        send={"send_success": 1},
        model="m1",
    )
    rec.record(record)

    raw = next((tmp_path / "v").glob("*.jsonl")).read_text(encoding="utf-8")
    # 隐私断言
    assert "67890" not in raw
    assert "12345" not in raw
    assert "学姐帮我看看这个" not in raw
    assert "你是学姐" not in raw
    # 结构断言
    payload = json.loads(raw.strip())
    assert payload["trace_id"] == "tr-1"
    assert payload["execution_mode"] == "shadow"
    assert payload["message"]["length"] == len("学姐帮我看看这个")
    assert payload["prompt"]["section_count"] == 2
    assert payload["mismatches"] == []


def test_recorder_summary(tmp_path) -> None:
    store = ValidationStore(tmp_path / "v", enabled=True)
    rec = ValidationRecorder(store)
    for i in range(3):
        rec.record_request(
            trace_id=f"t{i}",
            user_id="1",
            group_id="2",
            execution_mode="shadow",
            user_text="hi",
            latency={"total": 1.0 + i},
        )
    out = rec.summary()
    assert out["requests"] == 3
    assert out["latency"]["count"] == 3


def test_recorder_counter_tracks_critical(tmp_path) -> None:
    store = ValidationStore(tmp_path / "v", enabled=True)
    rec = ValidationRecorder(store)
    rec.record_request(
        trace_id="t",
        user_id="1",
        execution_mode="shadow",
        user_text="禁言他",
        legacy=BehaviorSnapshot(tool_calls=["mute"], permission_denied=[],
                                stop_reason="finished", ok=True),
        runtime=BehaviorSnapshot(tool_calls=[], permission_denied=["mute"],
                                 stop_reason="finished", ok=True),
    )
    assert rec.counters["critical"] >= 1


def test_validation_dir_separate_from_production(tmp_path, monkeypatch) -> None:
    """第三十六节：Validation 数据目录必须独立于生产数据。"""
    monkeypatch.setenv("DL_SENPAI_VALIDATION_DIR", str(tmp_path / "validation"))
    store = ValidationStore()
    assert "validation" in str(store.directory)
    assert "dl_senpai" not in str(store.directory)


def test_default_expected_differences_empty() -> None:
    """没有真实数据前不应预设"已知差异"。"""
    from dl_senpai.validation import default_expected_differences

    assert default_expected_differences() == set()
