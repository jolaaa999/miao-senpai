"""工具层：定义 / 注册表 / 解析适配器 / 执行器。

    from ..agent.tools import ToolDefinition, get_tool_registry, ToolCall
"""

from __future__ import annotations

from .executor import ExecutionOutcome, ToolExecutor
from .marker_adapter import (
    MarkerToolAdapter,
    NativeToolAdapter,
    ParseOutput,
    ToolCallAdapter,
)
from .models import Continuation, ToolCall, ToolCallSource, ToolResult
from .registry import (
    FailurePolicy,
    MarkerParser,
    ToolDefinition,
    ToolRegistry,
    VisibilityPredicate,
    get_tool_registry,
)
from .registry import ToolExecutor as ToolExecutorFn

__all__ = [
    "ToolDefinition",
    "ToolRegistry",
    "FailurePolicy",
    "get_tool_registry",
    "ToolCall",
    "ToolResult",
    "ToolCallSource",
    "Continuation",
    "ToolExecutor",
    "ExecutionOutcome",
    "ToolCallAdapter",
    "MarkerToolAdapter",
    "NativeToolAdapter",
    "ParseOutput",
    "MarkerParser",
    "VisibilityPredicate",
    "ToolExecutorFn",
]
