"""指标聚合：把 Validation 记录汇总成可读报告。

Phase 7A 第十六~二十三节。

设计要点：
    1. **不输出"总匹配率"**（第十四节）——只报 mismatch 的
       类型分布与严重度分布，让人自己判断风险。
    2. 延迟必须分阶段（第十九节）——"请求花了 5.3s"没有诊断价值，
       `queue_wait=3.1s / model=1.8s` 才有。
    3. 所有分群维度用**匿名 id**（第十七节）。
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from .models import Severity


# ================================================================
# 百分位（不引依赖，手写线性插值）
# ================================================================


def percentile(values: list[float], p: float) -> float:
    """线性插值百分位。

    为什么不用 statistics.quantiles：
        它的分位定义与业界 P95/P99 惯例不同（按 n 等分），
        会让 "P95=5.2s" 这类数字和监控系统对不上。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    k = (len(ordered) - 1) * (p / 100.0)
    low = math.floor(k)
    high = math.ceil(k)
    if low == high:
        return float(ordered[int(k)])
    return float(ordered[low] * (high - k) + ordered[high] * (k - low))


def summarize_latency(values: list[float]) -> dict[str, float]:
    """一组延迟的统计。第十六节要求 P50/P95/P99。"""
    if not values:
        return {"count": 0, "p50": 0.0, "p95": 0.0, "p99": 0.0, "max": 0.0}
    return {
        "count": len(values),
        "p50": round(percentile(values, 50), 2),
        "p95": round(percentile(values, 95), 2),
        "p99": round(percentile(values, 99), 2),
        "max": round(max(values), 2),
    }


# ================================================================
# 汇总
# ================================================================


def summarize(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """把记录汇总成 validation summary（第十六节）。"""
    rows = list(records)
    out: dict[str, Any] = {
        "requests": len(rows),
        "runtime_success_rate": 0.0,
        "legacy_success_rate": 0.0,
        "mismatch_by_type": {},
        "mismatch_by_severity": {s.value: 0 for s in Severity},
        "tool_calls": {},
        "tool_failure": {},
        "model_errors": 0,
        "latency": {},
        "latency_stages": {},
        "tokens": {
            "prompt": 0, "completion": 0, "total": 0, "per_request": 0.0,
        },
        "queue": {},
        "memory": {},
        "send": {},
        "by_group": {},
        "by_trigger": {},
        "by_model": {},
    }
    if not rows:
        return out

    runtime_ok = legacy_ok = 0
    total_latency: list[float] = []
    stage_values: dict[str, list[float]] = {}
    prompt_tokens = completion_tokens = total_tokens = 0
    model_calls = 0
    model_errors = 0

    for row in rows:
        legacy = row.get("legacy") or {}
        runtime = row.get("runtime") or {}

        if legacy.get("ok"):
            legacy_ok += 1
        if runtime.get("ok"):
            runtime_ok += 1

        # mismatch 分布
        for m in row.get("mismatches") or []:
            mtype = m.get("type", "unknown")
            sev = m.get("severity", "medium")
            out["mismatch_by_type"][mtype] = (
                out["mismatch_by_type"].get(mtype, 0) + 1
            )
            if sev in out["mismatch_by_severity"]:
                out["mismatch_by_severity"][sev] += 1

        # 工具调用（以 runtime 为准 —— 它是未来要保留的那条路径）
        for name in (runtime.get("tool_calls") or []):
            out["tool_calls"][name] = out["tool_calls"].get(name, 0) + 1

        # 延迟
        lat = row.get("latency") or {}
        if lat.get("total"):
            total_latency.append(float(lat["total"]))
        for stage, value in lat.items():
            if stage == "total" or not isinstance(value, (int, float)):
                continue
            stage_values.setdefault(stage, []).append(float(value))

        # token
        prompt_tokens += int(runtime.get("prompt_tokens") or 0)
        completion_tokens += int(runtime.get("completion_tokens") or 0)
        total_tokens += int(runtime.get("total_tokens") or 0)
        model_calls += int(runtime.get("model_call_count") or 0)

        # Phase 7A.1：若 telemetry 提供了 model_calls 明细，以明细为准。
        # 明细只累加 provider-reported 的非 None 值（第三十节），
        # 比快照粗略值准确；这里做**替换**而非叠加，避免双份。
        detail_calls = row.get("model_calls") or []
        if detail_calls:
            detail_total = sum(
                int(c["total_tokens"])
                for c in detail_calls
                if c.get("total_tokens") is not None
            )
            detail_prompt = sum(int(c.get("prompt_tokens") or 0) for c in detail_calls)
            detail_completion = sum(
                int(c.get("completion_tokens") or 0) for c in detail_calls
            )
            total_tokens += detail_total - int(runtime.get("total_tokens") or 0)
            prompt_tokens += detail_prompt - int(runtime.get("prompt_tokens") or 0)
            completion_tokens += (
                detail_completion - int(runtime.get("completion_tokens") or 0)
            )
            model_calls += len(detail_calls) - int(
                runtime.get("model_call_count") or 0
            )
            if any(c.get("total_tokens") is None for c in detail_calls):
                out["token_unknown_calls"] = out.get("token_unknown_calls", 0) + 1

        if runtime.get("error_class") == "model_error":
            model_errors += 1

        # 分群（第十七节，全部用匿名 id）
        ghash = row.get("group_hash") or ""
        if ghash:
            bucket = out["by_group"].setdefault(
                ghash, {"requests": 0, "critical": 0}
            )
            bucket["requests"] += 1
            for m in row.get("mismatches") or []:
                if m.get("severity") == Severity.CRITICAL.value:
                    bucket["critical"] += 1

        trigger = row.get("trigger_reason") or "unknown"
        out["by_trigger"][trigger] = out["by_trigger"].get(trigger, 0) + 1

        model = (row.get("prompt") or {}).get("model") or "unknown"
        out["by_model"][model] = out["by_model"].get(model, 0) + 1

    n = len(rows)
    out["runtime_success_rate"] = round(runtime_ok / n * 100, 2)
    out["legacy_success_rate"] = round(legacy_ok / n * 100, 2)
    out["model_errors"] = model_errors
    out["model_calls"] = model_calls
    out["latency"] = summarize_latency(total_latency)
    out["latency_stages"] = {
        stage: summarize_latency(values)
        for stage, values in sorted(stage_values.items())
    }
    out["tokens"] = {
        "prompt": prompt_tokens,
        "completion": completion_tokens,
        "total": total_tokens,
        "per_request": round(total_tokens / n, 1),
    }
    return out


# ================================================================
# 会话队列（第十八节）
# ================================================================


def summarize_queue(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """队列指标：等待时间、深度、拒绝数、活跃会话。"""
    waits: list[float] = []
    depths: list[float] = []
    rejected = 0
    sessions: set[str] = set()

    for row in records:
        lat = row.get("latency") or {}
        if "queue_wait" in lat:
            waits.append(float(lat["queue_wait"]))
        if "queue_depth" in lat:
            depths.append(float(lat["queue_depth"]))
        if row.get("error_class") == "queue_rejected":
            rejected += 1
        shash = row.get("session_hash")
        if shash:
            sessions.add(shash)

    return {
        "queue_wait": summarize_latency(waits),
        "queue_depth": {
            "max": max(depths) if depths else 0,
            "avg": round(sum(depths) / len(depths), 2) if depths else 0,
        },
        "rejected": rejected,
        "active_sessions": len(sessions),
    }


# ================================================================
# 工具健康（第二十一/二十二节）
# ================================================================


def summarize_tools(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """每个工具的成功/失败/超时/拒绝与延迟百分位。"""
    out: dict[str, Any] = {}

    def bucket(name: str) -> dict[str, Any]:
        return out.setdefault(
            name,
            {
                "calls": 0, "success": 0, "failure": 0,
                "timeout": 0, "permission_denied": 0,
                "durations": [],
                # 危险工具专用（第二十二节）
                "visibility_denied": 0, "execution_denied": 0,
            },
        )

    for row in records:
        for side in ("legacy", "runtime"):
            snap = row.get(side) or {}
            denied = set(snap.get("permission_denied") or [])
            for name in snap.get("tool_calls") or []:
                b = bucket(name)
                b["calls"] += 1
                if name in denied:
                    b["execution_denied"] += 1
                # 成功/失败由 error_class 与工具结果共同决定；
                # 记录层只带 error_class，因此这里按错误类别归类
                ec = snap.get("error_class") or ""
                if ec == "tool_timeout":
                    b["timeout"] += 1
                    b["failure"] += 1
                elif ec == "tool_error":
                    b["failure"] += 1
                elif ec == "permission_denied" and name in denied:
                    b["permission_denied"] += 1
                elif snap.get("ok"):
                    b["success"] += 1

            # 可见性被拒：工具存在但没进 prompt（第二十二节）
            visible = set(snap.get("visible_tools") or [])
            for name in ("mute", "card"):
                if name not in visible and name in denied:
                    bucket(name)["visibility_denied"] += 1

    # 收尾：算百分位并去掉原始数组
    for name, b in out.items():
        durations = b.pop("durations", [])
        b["duration"] = summarize_latency(durations)
        calls = b["calls"] or 0
        b["failure_rate"] = (
            round(b["failure"] / calls * 100, 2) if calls else 0.0
        )
    return out


# ================================================================
# Memory（第二十三节）
# ================================================================


def summarize_model_calls(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """按模型聚合调用（Phase 7A.1 第二十九/三十节）。

    返回每个模型的 calls / errors / retries / 延迟百分位 /
    token 合计。**token 未知的调用单独标记**，不与 0 混淆。
    """
    buckets: dict[str, dict[str, Any]] = {}

    def bucket(name: str) -> dict[str, Any]:
        return buckets.setdefault(
            name,
            {
                "model": name, "calls": 0, "errors": 0, "retries": 0,
                "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                "unknown_tokens": 0, "durations": [],
            },
        )

    for row in records:
        for call in row.get("model_calls") or []:
            b = bucket(str(call.get("model") or "unknown"))
            b["calls"] += 1
            if call.get("error"):
                b["errors"] += 1
            b["retries"] += int(call.get("retry_count") or 0)
            if call.get("prompt_tokens") is not None:
                b["prompt_tokens"] += int(call["prompt_tokens"])
            if call.get("completion_tokens") is not None:
                b["completion_tokens"] += int(call["completion_tokens"])
            if call.get("total_tokens") is None:
                # 第三十节：provider 未返回时明确标记，不当作 0
                b["unknown_tokens"] += 1
            else:
                b["total_tokens"] += int(call["total_tokens"])
            if call.get("latency_ms"):
                b["durations"].append(float(call["latency_ms"]))

    out: list[dict[str, Any]] = []
    for b in buckets.values():
        durations = b.pop("durations", [])
        b["latency"] = summarize_latency(durations)
        b["p95"] = b["latency"]["p95"]
        out.append(b)
    out.sort(key=lambda item: -item["calls"])
    return out


def summarize_memory(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """MemoryFacade 的命中统计（**不含内容**）。"""
    out = {
        "history_count": summarize_latency([]),
        "recalled_count": summarize_latency([]),
        "person_profile_hit": 0,
        "group_style_hit": 0,
        "affection_loaded": 0,
        "inner_state_loaded": 0,
        "scope_violations": 0,
    }
    history: list[float] = []
    recalled: list[float] = []

    for row in records:
        mem = row.get("memory") or {}
        if not mem:
            continue
        history.append(float(mem.get("history_count") or 0))
        recalled.append(float(mem.get("recalled_count") or 0))
        for key in ("person_profile_hit", "group_style_hit",
                    "affection_loaded", "inner_state_loaded"):
            if mem.get(key):
                out[key] += 1
        if mem.get("scope_violation"):
            out["scope_violations"] += 1

    out["history_count"] = summarize_latency(history)
    out["recalled_count"] = summarize_latency(recalled)
    return out


# ================================================================
# 发送侧（第三十二/三十三节）
# ================================================================


def summarize_send(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """QQ 发送成功率与媒体降级统计。"""
    out = {
        "send_success": 0,
        "send_failure": 0,
        "retry": 0,
        "forward_message": 0,
        "media_send_failure": 0,
        "fallback_text": 0,
        "success_rate": 0.0,
        "per_group": {},
    }
    for row in records:
        send = row.get("send") or {}
        if not send:
            continue
        for key in ("send_success", "send_failure", "retry",
                    "forward_message", "media_send_failure", "fallback_text"):
            out[key] += int(send.get(key) or 0)
        ghash = row.get("group_hash")
        if ghash:
            bucket = out["per_group"].setdefault(ghash, 0)
            out["per_group"][ghash] = bucket + int(send.get("send_success") or 0)

    total = out["send_success"] + out["send_failure"]
    out["success_rate"] = round(out["send_success"] / total * 100, 2) if total else 0.0
    return out


__all__ = [
    "percentile",
    "summarize_latency",
    "summarize",
    "summarize_queue",
    "summarize_tools",
    "summarize_model_calls",
    "summarize_memory",
    "summarize_send",
]
