"""Agent 运行时：Context / State / Result / Runtime / Guard / Gateway。

    from ..agent import AgentRuntime, AgentContext, get_tool_registry
    from ..agent.tools import ToolDefinition, ToolCall, ToolResult
"""

from __future__ import annotations

from .context import AgentContext, AgentResult, AgentState
from .entry import AgentEntry, AgentOutcome
from .guard import AgentGuard, GuardConfig, StopReason, TokenUsage
from .journal import ExecutionJournal, JournalEntry, SideEffectStatus
from .memory_facade import MemoryFacade, MemoryRef, MemorySnapshot, MemoryScope
from .mode import (
    AgentExecutionMode,
    canary_groups,
    mode_for_group,
    resolve_mode,
    should_fallback,
)
from .model_gateway import (
    LegacyModelGateway,
    ModelGateway,
    ModelRequest,
    ModelResponse,
    StubModelGateway,
)
from .permissions import (
    AllowAllPermissionService,
    DenyAllPermissionService,
    PermissionDecision,
    PermissionService,
)
from .recovery import RecoveryPolicy, build_recovery_policies
from .response import ResponseOutcome, ResponsePipeline, ResponsePlan
from .runtime import AgentRuntime

__all__ = [
    # 运行时
    "AgentRuntime",
    "AgentEntry",
    "AgentOutcome",
    "AgentContext",
    "AgentState",
    "AgentResult",
    "AgentGuard",
    "GuardConfig",
    "TokenUsage",
    # 模式
    "AgentExecutionMode",
    "resolve_mode",
    "mode_for_group",
    "canary_groups",
    "should_fallback",
    # 停止原因（Phase 4 的 guard.StopReason 保留兼容，Phase 5 用 mode.StopReason）
    "StopReason",
    # 副作用安全
    "ExecutionJournal",
    "JournalEntry",
    "SideEffectStatus",
    # 权限
    "PermissionService",
    "PermissionDecision",
    "AllowAllPermissionService",
    "DenyAllPermissionService",
    # 记忆
    "MemoryFacade",
    "MemorySnapshot",
    "MemoryRef",
    "MemoryScope",
    # 模型边界
    "ModelGateway",
    "LegacyModelGateway",
    "StubModelGateway",
    "ModelRequest",
    "ModelResponse",
    # 输出
    "ResponsePipeline",
    "ResponsePlan",
    "ResponseOutcome",
    "RecoveryPolicy",
    "build_recovery_policies",
]
