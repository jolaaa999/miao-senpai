"""Gateway 层：把平台事件归一化为 `MessageContext`。

    from ..gateway import from_onebot_event, MessageContext, agent_session_key

本层只做翻译，不做路由。路由属于 `core.router`。
"""

from __future__ import annotations

from .context import (
    NOTICE_GROUP_DECREASE,
    NOTICE_GROUP_INCREASE,
    NOTICE_OTHER,
    SESSION_GROUP,
    SESSION_PRIVATE,
    MessageContext,
    MessageType,
    agent_session_key,
    session_id_for_group,
    session_id_for_private,
)
from .onebot_adapter import from_onebot_event

__all__ = [
    "MessageContext",
    "MessageType",
    "from_onebot_event",
    "session_id_for_group",
    "session_id_for_private",
    "agent_session_key",
    "SESSION_GROUP",
    "SESSION_PRIVATE",
    "NOTICE_GROUP_INCREASE",
    "NOTICE_GROUP_DECREASE",
    "NOTICE_OTHER",
]
