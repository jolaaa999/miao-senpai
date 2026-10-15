"""Validation 报告 CLI（Phase 7A 第四十二/四十三节）。

用法：
    python scripts/validation_report.py                # 最近 24 小时
    python scripts/validation_report.py --window 7d    # 最近 7 天
    python scripts/validation_report.py --json         # 输出原始 JSON
    python scripts/validation_report.py --cleanup      # 按保留期清理

**不硬编码任何数字**（第四十三节）—— 全部来自真实数据。
没有数据时如实显示 0，而不是伪造示例。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "plugins"))
sys.path.insert(0, str(ROOT))

WINDOWS = {
    "1h": 3600,
    "24h": 86400,
    "7d": 86400 * 7,
    "30d": 86400 * 30,
}


def parse_window(value: str) -> float:
    """解析时间窗口（如 24h / 7d）。"""
    if value in WINDOWS:
        return float(WINDOWS[value])
    if value.endswith("h"):
        return float(value[:-1]) * 3600
    if value.endswith("d"):
        return float(value[:-1]) * 86400
    if value.endswith("m"):
        return float(value[:-1]) * 60
    return float(value)


def fmt_pct(value: float) -> str:
    return f"{value:.1f}%"


def instrumentation_coverage(rows: list[dict]) -> dict[str, str]:
    """插桩覆盖情况（第五十七节）。

    **不是百分比** —— 只回答"这一块是否已接入生产路径"。
    第五十六节：必须能区分 `no tool calls`（真实没有调用）
    与 `tool metrics not instrumented`（字段根本没人填）。
    """
    if not rows:
        return {
            "queue": "no data", "model": "no data", "tool": "no data",
            "memory": "no data", "send": "no data", "cost": "no data",
        }

    def any_key(key: str) -> bool:
        return any(key in row for row in rows)

    def any_nonempty(key: str) -> bool:
        return any(bool(row.get(key)) for row in rows)

    return {
        # queue 字段由 SessionExecutor 插桩回填
        "queue": "active" if any_key("queue") else "NOT INSTRUMENTED",
        # model_calls 由 llm._complete_once 回填
        "model": "active" if any_nonempty("model_calls") else "no calls",
        # tools 由 ToolExecutor 回填
        "tool": "active" if any_key("tools") else "NOT INSTRUMENTED",
        # memory 由 MemoryFacade 回填
        "memory": "active" if any_nonempty("memory") else "NOT INSTRUMENTED",
        # send 由 reply_send 回填
        "send": "active" if any_nonempty("send") else "NOT INSTRUMENTED",
        # cost 需要配置价格才有值，否则 unknown（第三十一节）
        "cost": "unknown (price not configured)",
    }


def render(summary: dict, *, window: str, real_samples: int,
           coverage: dict[str, str] | None = None) -> str:
    """渲染人类可读报告（第四十三节的结构）。"""
    lines: list[str] = []
    add = lines.append

    add("=" * 62)
    add(f"Validation Report — last {window}")
    add("=" * 62)

    requests = summary.get("requests", 0)
    add("")
    add(f"Requests:            {requests}")
    if requests == 0:
        add("")
        add("  （无数据）")
        add("")
        add("  这可能意味着：")
        add("    1. 未启用采集：设置 DL_SENPAI_VALIDATION=1")
        add("    2. 未运行 SHADOW 模式：DL_SENPAI_AGENT_MODE=shadow")
        add("    3. 该时间窗口内确实没有请求")
        add("")
        add("  注意：本报告只统计真实运行数据。")
        add(f"  real traffic samples = {real_samples}")
        return "\n".join(lines)

    add(f"Runtime success:     {fmt_pct(summary.get('runtime_success_rate', 0))}")
    add(f"Legacy success:      {fmt_pct(summary.get('legacy_success_rate', 0))}")

    # -- 差异（按严重度，不报"总匹配率" —— 第十四节）--
    add("")
    add("-" * 62)
    add("Mismatches by severity")
    add("-" * 62)
    sev = summary.get("mismatch_by_severity", {})
    for level in ("critical", "high", "medium", "low", "info"):
        count = sev.get(level, 0)
        marker = "  <-- 需要关注" if level in ("critical", "high") and count else ""
        add(f"  {level:<10} {count}{marker}")

    by_type = summary.get("mismatch_by_type", {})
    if by_type:
        add("")
        add("Mismatches by type")
        for name, count in sorted(by_type.items(), key=lambda kv: -kv[1]):
            add(f"  {name:<32} {count}")

    # -- 延迟（分阶段 —— 第十九节）--
    add("")
    add("-" * 62)
    add("Latency (ms)")
    add("-" * 62)
    lat = summary.get("latency", {})
    if lat.get("count"):
        add(f"  P50 {lat['p50']:.0f}   P95 {lat['p95']:.0f}   "
            f"P99 {lat['p99']:.0f}   max {lat['max']:.0f}")
    add("")
    add("  by stage:")
    stages = summary.get("latency_stages", {})
    if stages:
        for stage, st in stages.items():
            add(f"    {stage:<14} P50 {st['p50']:>7.0f}  P95 {st['p95']:>7.0f}  "
                f"n={st['count']}")
    else:
        add("    （无分阶段数据）")

    # -- Token --
    add("")
    add("-" * 62)
    add("Tokens")
    add("-" * 62)
    tok = summary.get("tokens", {})
    add(f"  prompt {tok.get('prompt', 0)}  completion {tok.get('completion', 0)}  "
        f"total {tok.get('total', 0)}")
    add(f"  per request: {tok.get('per_request', 0)}")
    add(f"  model calls: {summary.get('model_calls', 0)}  "
        f"errors: {summary.get('model_errors', 0)}")

    # -- 工具 --
    add("")
    add("-" * 62)
    add("Tool calls")
    add("-" * 62)
    tools = summary.get("tool_calls", {})
    if tools:
        for name, count in sorted(tools.items(), key=lambda kv: -kv[1]):
            health = (summary.get("tools") or {}).get(name) or {}
            rate = health.get("failure_rate", 0)
            dur = (health.get("duration") or {}).get("p95", 0)
            add(f"  {name:<12} calls {count:<5} fail {rate:>5.1f}%  p95 {dur:.0f}ms")
    else:
        # 第五十六节：区分"真的没调用"与"没插桩"
        covered = (coverage or {}).get("tool", "")
        if covered == "active":
            add("  no tool calls in this window")
        else:
            add(f"  tool metrics {covered}")

    # -- 模型 / 成本 --
    add("")
    add("-" * 62)
    add("Model / Cost")
    add("-" * 62)
    mcalls = summary.get("metrics_model_calls", [])
    if mcalls:
        for item in mcalls[:10]:
            add(f"  {item.get('model', '?')}  calls {item.get('calls', 0)}  "
                f"err {item.get('errors', 0)}  retry {item.get('retries', 0)}  "
                f"p95 {item.get('p95', 0):.0f}ms")
    else:
        covered = (coverage or {}).get("model", "")
        add(f"  model metrics: {covered}")
    add(f"  cost: {summary.get('cost', 'unknown')}")
    if summary.get("tokens", {}).get("unknown"):
        add("  note: some calls had no provider-reported token usage")

    # -- 队列 --
    add("")
    add("-" * 62)
    add("Session queue")
    add("-" * 62)
    q = summary.get("queue", {})
    if q:
        wait = q.get("queue_wait", {})
        depth = q.get("queue_depth", {})
        add(f"  wait P50 {wait.get('p50', 0):.0f}ms  P95 {wait.get('p95', 0):.0f}ms")
        add(f"  depth max {depth.get('max', 0)}  avg {depth.get('avg', 0)}")
        add(f"  rejected {q.get('rejected', 0)}  "
            f"active sessions {q.get('active_sessions', 0)}")

    # -- Memory --
    add("")
    add("-" * 62)
    add("Memory")
    add("-" * 62)
    mem = summary.get("memory", {})
    if mem:
        add(f"  person_profile_hit {mem.get('person_profile_hit', 0)}  "
            f"group_style_hit {mem.get('group_style_hit', 0)}")
        add(f"  affection_loaded {mem.get('affection_loaded', 0)}  "
            f"inner_state_loaded {mem.get('inner_state_loaded', 0)}")
        violations = mem.get("scope_violations", 0)
        add(f"  scope violations: {violations}"
            + ("  <-- CRITICAL" if violations else ""))

    # -- 发送 --
    add("")
    add("-" * 62)
    add("QQ send")
    add("-" * 62)
    snd = summary.get("send", {})
    if snd:
        add(f"  success {snd.get('send_success', 0)}  "
            f"failure {snd.get('send_failure', 0)}  "
            f"rate {fmt_pct(snd.get('success_rate', 0))}")
        add(f"  retry {snd.get('retry', 0)}  forward {snd.get('forward_message', 0)}  "
            f"media_fail {snd.get('media_send_failure', 0)}  "
            f"fallback {snd.get('fallback_text', 0)}")

    # -- 分群 --
    by_group = summary.get("by_group", {})
    if by_group:
        add("")
        add("-" * 62)
        add("Top groups (anonymized)")
        add("-" * 62)
        ranked = sorted(by_group.items(), key=lambda kv: -kv[1]["requests"])[:10]
        for ghash, info in ranked:
            add(f"  {ghash}  requests {info['requests']}  "
                f"critical {info['critical']}")

    add("")
    add("-" * 62)
    add("Instrumentation coverage")
    add("-" * 62)
    for area, status in (coverage or {}).items():
        add(f"  {area:<10} {status}")

    add("")
    add("-" * 62)
    add(f"real traffic samples = {real_samples}")
    add("-" * 62)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validation report")
    parser.add_argument("--window", default="24h", help="24h / 7d / 30d / 1h")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    parser.add_argument("--cleanup", action="store_true", help="按保留期清理")
    parser.add_argument("--dir", default="", help="覆盖数据目录")
    args = parser.parse_args()

    if args.dir:
        os.environ["DL_SENPAI_VALIDATION_DIR"] = args.dir

    from dl_senpai.validation import ValidationStore, summarize
    from dl_senpai.validation import metrics as m

    store = ValidationStore()

    if args.cleanup:
        removed = store.cleanup()
        print(f"cleanup: removed {len(removed)} file(s)")
        for name in removed:
            print(f"  {name}")
        return 0

    seconds = parse_window(args.window)
    since = time.time() - seconds
    rows = store.read(since=since)

    summary = summarize(rows)
    summary["queue"] = m.summarize_queue(rows)
    summary["tools"] = m.summarize_tools(rows)
    summary["memory"] = m.summarize_memory(rows)
    summary["send"] = m.summarize_send(rows)
    summary["metrics_model_calls"] = m.summarize_model_calls(rows)

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    # 真实样本数 = 该窗口内的实际记录数（如实报告，不注水）
    print(render(summary, window=args.window, real_samples=len(rows),
                 coverage=instrumentation_coverage(rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
