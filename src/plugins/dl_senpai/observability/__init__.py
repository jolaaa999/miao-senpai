"""可观测性：trace 上下文 + 结构化日志 + 指标。

对外只需记住三个入口：

    from ..observability import trace_scope, log_event, metrics
"""

from __future__ import annotations

from .context import (
    TraceContext,
    current_trace,
    new_trace_id,
    trace_id,
    trace_scope,
    trace_stage,
)
from .logging import format_fields, log_event, log_llm_usage
from .metrics import Metrics, metrics

__all__ = [
    "TraceContext",
    "current_trace",
    "new_trace_id",
    "trace_id",
    "trace_scope",
    "trace_stage",
    "log_event",
    "log_llm_usage",
    "format_fields",
    "Metrics",
    "metrics",
]
