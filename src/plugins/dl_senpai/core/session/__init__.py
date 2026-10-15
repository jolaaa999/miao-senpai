"""Session 并发控制。

    from ..core.session import SessionExecutor, get_executor, agent_key_for

同 session FIFO 串行、不同 session 并行；队列满时 reject newest。
"""

from __future__ import annotations

from .executor import (
    QueueFullError,
    Reject,
    SessionExecutor,
    SubmitResult,
    get_executor,
    reset_executor,
)
from .keys import (
    agent_key_for,
    agent_session_key,
    memory_scope_for_group,
    memory_scope_for_private,
)

__all__ = [
    "SessionExecutor",
    "SubmitResult",
    "QueueFullError",
    "Reject",
    "get_executor",
    "reset_executor",
    "agent_key_for",
    "agent_session_key",
    "memory_scope_for_group",
    "memory_scope_for_private",
]
