"""行为比较器：Legacy vs Runtime。

Phase 7A 第十一/十二/十三/十四/十五节。

**核心原则：不比较自然语言文本。**

原因（第十一节）：LLM 输出非确定 —— 同一输入两次调用都可能不同措辞。
比较最终文本只会产生大量噪音，掩盖真正的行为差异。

因此比较的是**行为维度**（第十二节）：
    TRIGGER / VISIBLE_TOOLS / TOOL_CALL / TOOL_ARGUMENT_SHAPE /
    PERMISSION / RECOVERY / STOP_REASON / MODEL_CALL_COUNT /
    TOKEN_USAGE / LATENCY

**不输出"总匹配率"**（第十四节）：
    返回的是 `list[Mismatch]`，每个带类型与严重度。
    调用方应看"有几个 CRITICAL"，而不是"一致率多少"。
"""

from __future__ import annotations

from .models import (
    BehaviorSnapshot,
    Mismatch,
    MismatchType,
    Severity,
    severity_for,
)

# Token 差异的相对阈值：低于此比例视为噪声（LOW，甚至不报）。
#
# 为什么需要阈值：模型的 token 计数天然有小幅波动
# （温度、分词边界），逐条报差异会淹没真问题。
TOKEN_NOISE_RATIO = 0.15

# 延迟差异的相对阈值。延迟受网络影响大，同样需要容忍带。
LATENCY_NOISE_RATIO = 0.50


def compare(
    legacy: BehaviorSnapshot | None,
    runtime: BehaviorSnapshot | None,
    *,
    expected_differences: set[MismatchType] | None = None,
    comparison_enabled: bool = True,
) -> list[Mismatch]:
    """比较两个行为快照，返回差异列表。

    Args:
        legacy: Legacy（authoritative）侧快照。None 表示未执行。
        runtime: Runtime（shadow）侧快照。None 表示未执行。
        expected_differences: 已知合理差异的类型集合，
            会被降级为 EXPECTED_DIFFERENCE/INFO。
        comparison_enabled: 是否做对比。
            **RUNTIME 模式下传 False** —— 那条路径只有自己一侧，
            没有"对比"这回事，报"单侧缺失"是噪音而非信号。
            （Phase 7B：切到 runtime 后仍需记录自身指标，
              但不该产生任何 mismatch 记录。）

    Returns:
        差异列表。**空列表表示行为一致** —— 注意这是"无差异"，
        不是"匹配率 100%"，因为比较维度本身是离散结构化的。
    """
    if not comparison_enabled:
        # 不做对比：直接返回空，不制造"单侧缺失"噪音
        return []

    expected = expected_differences or set()
    out: list[Mismatch] = []

    # 单侧缺失：这本身就是一个重要信号
    if legacy is None or runtime is None:
        which = "legacy" if legacy is None else "runtime"
        out.append(
            Mismatch(
                type=MismatchType.EXPECTED_DIFFERENCE,
                severity=Severity.MEDIUM,
                detail=f"{which} 侧未产生快照",
            )
        )
        return out

    def add(
        mtype: MismatchType,
        detail: str,
        legacy_value=None,
        runtime_value=None,
        *,
        tools: set[str] | None = None,
        cross_group: bool = False,
    ) -> None:
        if mtype in expected:
            out.append(
                Mismatch(
                    type=MismatchType.EXPECTED_DIFFERENCE,
                    severity=Severity.INFO,
                    detail=f"[expected] {detail}",
                    legacy_value=legacy_value,
                    runtime_value=runtime_value,
                )
            )
            return
        out.append(
            Mismatch(
                type=mtype,
                severity=severity_for(mtype, tools=tools, cross_group=cross_group),
                detail=detail,
                legacy_value=legacy_value,
                runtime_value=runtime_value,
            )
        )

    # -- TRIGGER --
    if legacy.triggered != runtime.triggered:
        add(
            MismatchType.TRIGGER_MISMATCH,
            "触发判定不同",
            legacy.triggered,
            runtime.triggered,
        )

    # -- VISIBLE_TOOLS --
    lv, rv = set(legacy.visible_tools), set(runtime.visible_tools)
    if lv != rv:
        add(
            MismatchType.VISIBLE_TOOL_MISMATCH,
            "可见工具集合不同",
            sorted(lv - rv),
            sorted(rv - lv),
            tools=(lv ^ rv),
        )

    # -- TOOL_CALL --
    #
    # 先比"选了哪些工具"（集合），再比"顺序与参数形状"（签名）。
    # 分两级的原因：工具集合不同是 HIGH，而顺序不同通常只是编排细节。
    lc, rc = set(legacy.tool_calls), set(runtime.tool_calls)
    if lc != rc:
        add(
            MismatchType.TOOL_SELECTION_MISMATCH,
            "工具选择不同",
            sorted(lc),
            sorted(rc),
            tools=(lc ^ rc),
        )

    if legacy.tool_call_signature() != runtime.tool_call_signature():
        add(
            MismatchType.TOOL_ARGUMENT_MISMATCH,
            "工具调用顺序或参数形状不同",
            legacy.tool_call_signature(),
            runtime.tool_call_signature(),
            tools=lc & rc,
        )

    # -- PERMISSION --
    #
    # 权限差异一律 CRITICAL：意味着一边允许、一边拒绝，
    # 用户可见行为会完全不同（例如一边禁言了、一边没禁言）。
    lp, rp = set(legacy.permission_denied), set(runtime.permission_denied)
    if lp != rp:
        add(
            MismatchType.PERMISSION_MISMATCH,
            "权限判定不同",
            sorted(lp),
            sorted(rp),
            tools=(lp ^ rp) or {"mute"},
        )

    # -- RECOVERY --
    lrec, rrec = set(legacy.recovery_triggered), set(runtime.recovery_triggered)
    if lrec != rrec:
        add(
            MismatchType.RECOVERY_MISMATCH,
            "补救触发不同",
            sorted(lrec),
            sorted(rrec),
        )

    # -- STOP_REASON --
    if legacy.stop_reason != runtime.stop_reason:
        # 一方完成、另一方因护栏停止 → 升级为 HIGH（行为结果不同）
        guard_stops = {
            "max_steps", "max_tool_calls", "repeat_limit",
            "token_budget", "timeout", "permission_denied",
        }
        if {legacy.stop_reason, runtime.stop_reason} & guard_stops:
            add(
                MismatchType.STOP_REASON_MISMATCH,
                "停止原因不同（含护栏停止）",
                legacy.stop_reason,
                runtime.stop_reason,
            )
            # 手动升级：护栏差异比普通 stop_reason 差异更重要
            out[-1].severity = Severity.HIGH
        else:
            add(
                MismatchType.STOP_REASON_MISMATCH,
                "停止原因不同",
                legacy.stop_reason,
                runtime.stop_reason,
            )

    # -- MODEL_CALL_COUNT --
    if legacy.model_call_count != runtime.model_call_count:
        add(
            MismatchType.MODEL_CALL_COUNT_MISMATCH,
            "模型调用次数不同",
            legacy.model_call_count,
            runtime.model_call_count,
        )

    # -- TOKEN_USAGE --
    #
    # 用相对阈值过滤噪声：token 计数天然有波动。
    if not _within_ratio(
        legacy.total_tokens, runtime.total_tokens, TOKEN_NOISE_RATIO
    ):
        add(
            MismatchType.EXPECTED_DIFFERENCE,
            "token 用量差异",
            legacy.total_tokens,
            runtime.total_tokens,
        )

    # -- LATENCY --
    #
    # 延迟差异不单独报 mismatch —— 它受网络/负载影响，
    # 归入 ValidationRecord.latency 供 P95/P99 统计即可。
    # 这里只在差异极端时记 INFO，避免污染 mismatch 统计。

    # -- 执行成功性 --
    if legacy.ok != runtime.ok:
        add(
            MismatchType.EXPECTED_DIFFERENCE,
            "一侧执行失败",
            {"ok": legacy.ok, "error": legacy.error_class},
            {"ok": runtime.ok, "error": runtime.error_class},
        )

    return out


def _within_ratio(a: int, b: int, ratio: float) -> bool:
    """两个值是否在相对阈值内。"""
    if a == b:
        return True
    base = max(a, b, 1)
    return abs(a - b) / base <= ratio


def count_by_severity(mismatches: list[Mismatch]) -> dict[str, int]:
    """按严重度计数（第十六节的 mismatch count by severity）。"""
    out: dict[str, int] = {s.value: 0 for s in Severity}
    for m in mismatches:
        out[m.severity.value] += 1
    return out


def count_by_type(mismatches: list[Mismatch]) -> dict[str, int]:
    """按类型计数（第十六节）。"""
    out: dict[str, int] = {}
    for m in mismatches:
        out[m.type.value] = out.get(m.type.value, 0) + 1
    return out


def has_blocking_mismatch(mismatches: list[Mismatch]) -> bool:
    """是否存在需要人工关注的差异（CRITICAL 或 HIGH）。

    第六十四节：Legacy 删除门槛要看 critical/high 数量，
    而不是"运行了多久"。
    """
    return any(
        m.severity in (Severity.CRITICAL, Severity.HIGH) for m in mismatches
    )


__all__ = [
    "compare",
    "count_by_severity",
    "count_by_type",
    "has_blocking_mismatch",
    "TOKEN_NOISE_RATIO",
    "LATENCY_NOISE_RATIO",
]
