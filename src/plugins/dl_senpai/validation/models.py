"""Validation 数据模型：记录、差异分类、严重度。

Phase 7A 第六/十二/十三/十五节。

设计要点：
    1. **不比较自然语言文本**（第十一节）—— LLM 非确定，
       比较最终措辞毫无意义。只比较行为维度。
    2. **不制造"总匹配率"**（第十四节）——
       "措辞不同"和"该禁言却没禁言"严重度天差地别，
       合并成一个百分比会掩盖真问题。
    3. 因此本模块的核心是 (mismatch type, severity) 二元组，
       而不是一个 score。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ================================================================
# 执行模式与停止原因（复用既有枚举，避免第二套定义）
# ================================================================


class ErrorClass(str, Enum):
    """统一错误分类（第三十节）。

    第三十一节：**不要所有异常都打 ERROR**。
    例如 PERMISSION_DENIED 往往是正常业务事件（模型想禁言但无权限），
    应按语义分级。分级见 `error_log_level()`。
    """

    MODEL_ERROR = "model_error"
    TOOL_ERROR = "tool_error"
    TOOL_TIMEOUT = "tool_timeout"
    PERMISSION_DENIED = "permission_denied"
    QUEUE_REJECTED = "queue_rejected"
    QQ_SEND_ERROR = "qq_send_error"
    MEMORY_ERROR = "memory_error"
    CONFIG_ERROR = "config_error"
    INFRA_ERROR = "infra_error"


# 每个错误类别对应的日志级别。
#
# 判断依据是"是否为正常业务路径"：
#   INFO    —— 正常业务分支（权限拒绝是设计内的结果）
#   WARNING —— 可恢复的外部故障（模型超时、发送失败有重试）
#   ERROR   —— 需要人介入的真实故障（配置错误、基础设施异常）
_ERROR_LEVEL: dict[ErrorClass, str] = {
    ErrorClass.PERMISSION_DENIED: "info",
    ErrorClass.QUEUE_REJECTED: "info",
    ErrorClass.TOOL_TIMEOUT: "warning",
    ErrorClass.QQ_SEND_ERROR: "warning",
    ErrorClass.MODEL_ERROR: "warning",
    ErrorClass.TOOL_ERROR: "warning",
    ErrorClass.MEMORY_ERROR: "warning",
    ErrorClass.CONFIG_ERROR: "error",
    ErrorClass.INFRA_ERROR: "error",
}


def error_log_level(error_class: ErrorClass | str) -> str:
    """该错误类别应使用的日志级别（第三十一节）。"""
    if isinstance(error_class, str):
        try:
            error_class = ErrorClass(error_class)
        except ValueError:
            return "warning"
    return _ERROR_LEVEL.get(error_class, "warning")


# ================================================================
# 差异分类与严重度
# ================================================================


class MismatchType(str, Enum):
    """行为差异类型（第十三节）。"""

    TRIGGER_MISMATCH = "trigger_mismatch"
    VISIBLE_TOOL_MISMATCH = "visible_tool_mismatch"
    TOOL_SELECTION_MISMATCH = "tool_selection_mismatch"
    TOOL_ARGUMENT_MISMATCH = "tool_argument_mismatch"
    PERMISSION_MISMATCH = "permission_mismatch"
    RECOVERY_MISMATCH = "recovery_mismatch"
    STOP_REASON_MISMATCH = "stop_reason_mismatch"
    MODEL_CALL_COUNT_MISMATCH = "model_call_count_mismatch"
    # 已知合理差异（例如 Legacy 没有 Knowledge Tool 而 Runtime 有）
    EXPECTED_DIFFERENCE = "expected_difference"


class Severity(str, Enum):
    """差异严重度（第十五节）。

    刻意不做数值化加权 —— 那会诱导出"总匹配率"这种假指标。
    """

    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


# 类型 → 基础严重度映射。
#
# 依据第十五节的示例：
#   CRITICAL —— 权限差异 / 危险 Tool 差异 / 重复副作用风险 / 跨群 Memory 污染
#   HIGH     —— Tool 选择完全不同 / Trigger 差异
#   MEDIUM   —— Recovery 差异
#   LOW      —— Token 差异 / 非关键模型调用次数差异
_BASE_SEVERITY: dict[MismatchType, Severity] = {
    MismatchType.PERMISSION_MISMATCH: Severity.CRITICAL,
    MismatchType.TOOL_SELECTION_MISMATCH: Severity.HIGH,
    MismatchType.TRIGGER_MISMATCH: Severity.HIGH,
    MismatchType.RECOVERY_MISMATCH: Severity.MEDIUM,
    MismatchType.TOOL_ARGUMENT_MISMATCH: Severity.MEDIUM,
    MismatchType.VISIBLE_TOOL_MISMATCH: Severity.LOW,
    MismatchType.STOP_REASON_MISMATCH: Severity.LOW,
    MismatchType.MODEL_CALL_COUNT_MISMATCH: Severity.LOW,
    MismatchType.EXPECTED_DIFFERENCE: Severity.INFO,
}

# 被视为"危险工具"的集合。这些工具的差异一律升级为 CRITICAL
# （因为它们有过可见的对外副作用：禁言别人、改别人名片）。
DANGEROUS_TOOLS = {"mute", "card"}


def severity_for(
    mismatch: MismatchType,
    *,
    tools: set[str] | None = None,
    cross_group: bool = False,
) -> Severity:
    """计算实际严重度（可在基础级别上升级）。

    升级规则（第十五节 CRITICAL 的定义）：
        - 涉及危险工具（mute / card）→ CRITICAL
        - 跨群 Memory 污染 → CRITICAL
    """
    if cross_group:
        return Severity.CRITICAL
    if tools and (tools & DANGEROUS_TOOLS):
        return Severity.CRITICAL
    return _BASE_SEVERITY.get(mismatch, Severity.MEDIUM)


# ================================================================
# 行为快照（用于比较）
# ================================================================


@dataclass
class BehaviorSnapshot:
    """一次执行的行为摘要（第十二节的比较维度）。

    **不含任何自然语言文本** —— 只留结构与计数。
    """

    # TRIGGER
    trigger_reason: str = ""
    triggered: bool = False

    # VISIBLE_TOOLS
    visible_tools: list[str] = field(default_factory=list)

    # TOOL_CALL / TOOL_ARGUMENT_SHAPE
    tool_calls: list[str] = field(default_factory=list)
    # 工具名 → 参数**键名集合**（只看形状，不看值）
    tool_argument_shapes: dict[str, list[str]] = field(default_factory=dict)

    # PERMISSION
    permission_denied: list[str] = field(default_factory=list)

    # RECOVERY
    recovery_triggered: list[str] = field(default_factory=list)

    # STOP_REASON
    stop_reason: str = ""

    # MODEL_CALL_COUNT / TOKEN_USAGE
    model_call_count: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    # LATENCY
    latency_ms: float = 0.0

    # 执行是否成功（无异常）
    ok: bool = True
    error_class: str = ""

    def tool_call_signature(self) -> list[str]:
        """工具调用的**有序签名**（含参数键形状，不含值）。"""
        out: list[str] = []
        for name in self.tool_calls:
            keys = sorted(self.tool_argument_shapes.get(name, []))
            out.append(f"{name}({','.join(keys)})")
        return out


@dataclass
class Mismatch:
    """一条行为差异。"""

    type: MismatchType
    severity: Severity
    detail: str = ""
    legacy_value: Any = None
    runtime_value: Any = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "severity": self.severity.value,
            "detail": self.detail,
            "legacy": _safe_value(self.legacy_value),
            "runtime": _safe_value(self.runtime_value),
        }


def _safe_value(value: Any) -> Any:
    """把比较值压成可安全落盘的形状（列表截断到 8 项）。"""
    if isinstance(value, (list, tuple)):
        items = list(value)[:8]
        return items
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:80]


# ================================================================
# 一次请求的完整验证记录
# ================================================================


@dataclass
class ValidationRecord:
    """一次真实请求的验证记录（第六节）。"""

    trace_id: str = ""
    # 匿名 id（第八节）——**不是**真实 QQ 号/群号
    session_hash: str = ""
    user_hash: str = ""
    group_hash: str = ""

    execution_mode: str = ""
    trigger_reason: str = ""

    # 用户消息结构画像（第九节，不含原文）
    message: dict[str, Any] = field(default_factory=dict)
    # Prompt 结构画像（第十节，不含内容）
    prompt: dict[str, Any] = field(default_factory=dict)

    legacy: BehaviorSnapshot | None = None
    runtime: BehaviorSnapshot | None = None

    mismatches: list[Mismatch] = field(default_factory=list)

    # 分阶段延迟（第十九节）
    latency: dict[str, float] = field(default_factory=dict)

    # 错误（第三十节）
    error_class: str = ""
    error_detail: str = ""

    timestamp: float = 0.0

    def worst_severity(self) -> Severity | None:
        """最严重的差异级别（便于快速筛选）。"""
        order = [
            Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM,
            Severity.LOW, Severity.INFO,
        ]
        present = {m.severity for m in self.mismatches}
        for sev in order:
            if sev in present:
                return sev
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "session_hash": self.session_hash,
            "user_hash": self.user_hash,
            "group_hash": self.group_hash,
            "execution_mode": self.execution_mode,
            "trigger_reason": self.trigger_reason,
            "message": self.message,
            "prompt": self.prompt,
            "legacy": _snapshot_dict(self.legacy),
            "runtime": _snapshot_dict(self.runtime),
            "mismatches": [m.as_dict() for m in self.mismatches],
            "latency": self.latency,
            "error_class": self.error_class,
            "error_detail": self.error_detail,
            "timestamp": self.timestamp,
        }


def _snapshot_dict(snap: BehaviorSnapshot | None) -> dict[str, Any] | None:
    if snap is None:
        return None
    return {
        "trigger_reason": snap.trigger_reason,
        "triggered": snap.triggered,
        "visible_tools": sorted(snap.visible_tools),
        "tool_calls": snap.tool_calls,
        "tool_argument_shapes": snap.tool_argument_shapes,
        "permission_denied": sorted(snap.permission_denied),
        "recovery_triggered": sorted(snap.recovery_triggered),
        "stop_reason": snap.stop_reason,
        "model_call_count": snap.model_call_count,
        "prompt_tokens": snap.prompt_tokens,
        "completion_tokens": snap.completion_tokens,
        "total_tokens": snap.total_tokens,
        "latency_ms": round(snap.latency_ms, 2),
        "ok": snap.ok,
        "error_class": snap.error_class,
    }


__all__ = [
    "ErrorClass",
    "error_log_level",
    "MismatchType",
    "Severity",
    "severity_for",
    "DANGEROUS_TOOLS",
    "BehaviorSnapshot",
    "Mismatch",
    "ValidationRecord",
]
