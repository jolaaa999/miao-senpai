"""Validation 层：真实运行数据的行为观测。

Phase 7A。核心思想：

    Measure → Understand → Fix
    而不是
    Guess   → Refactor   → Hope

**本层不参与业务决策** —— 它只观察。任何写入失败都被吞掉，
绝不影响回复（与 observability 层同一条原则）。

模块划分：

    privacy.py     真实 id / 文本 → 匿名与结构画像（所有数据的入口）
    models.py      记录、差异类型、严重度
    comparator.py  Legacy vs Runtime 的**行为**比较（不比自然语言）
    store.py       JSONL 存储 + 保留期清理
    metrics.py     聚合（延迟分阶段 / token / 队列 / 工具 / 内存 / 发送）
    recorder.py    统一入口
    cost.py        Token 成本（价格未知时明确 unknown）+ Memory 作用域守卫

**默认关闭**：未设 `DL_SENPAI_VALIDATION=1` 时不写任何文件。
"""

from __future__ import annotations

from .comparator import (
    compare,
    count_by_severity,
    count_by_type,
    has_blocking_mismatch,
)
from .cost import (
    CostTracker,
    ScopeCheck,
    check_scope,
    cost_tracker_from_config,
    memory_metrics,
)
from .metrics import (
    percentile,
    summarize,
    summarize_latency,
    summarize_memory,
    summarize_model_calls,
    summarize_queue,
    summarize_send,
    summarize_tools,
)
from .models import (
    BehaviorSnapshot,
    ErrorClass,
    Mismatch,
    MismatchType,
    Severity,
    ValidationRecord,
    error_log_level,
    severity_for,
)
from .privacy import (
    anonymize,
    content_hash,
    contains_secret,
    debug_content_enabled,
    message_profile,
    prompt_profile,
    redact_secrets,
)
from .recorder import (
    ValidationRecorder,
    default_expected_differences,
    get_recorder,
    reset_recorder,
)
from .store import ValidationStore
from .telemetry import (
    MemoryTelemetry,
    ModelCallTelemetry,
    RequestTelemetry,
    SendTelemetry,
    ToolOutcome,
    ToolTelemetry,
    bind_telemetry,
    current_telemetry,
    unbind_telemetry,
)

__all__ = [
    # privacy
    "anonymize",
    "content_hash",
    "contains_secret",
    "redact_secrets",
    "message_profile",
    "prompt_profile",
    "debug_content_enabled",
    # models
    "BehaviorSnapshot",
    "Mismatch",
    "MismatchType",
    "Severity",
    "ValidationRecord",
    "ErrorClass",
    "error_log_level",
    "severity_for",
    # comparator
    "compare",
    "count_by_severity",
    "count_by_type",
    "has_blocking_mismatch",
    # store
    "ValidationStore",
    # metrics
    "percentile",
    "summarize_latency",
    "summarize",
    "summarize_queue",
    "summarize_tools",
    "summarize_model_calls",
    "summarize_memory",
    "summarize_send",
    # recorder
    "ValidationRecorder",
    "get_recorder",
    "reset_recorder",
    "default_expected_differences",
    # cost / guard
    "CostTracker",
    "cost_tracker_from_config",
    "ScopeCheck",
    "check_scope",
    "memory_metrics",
]
