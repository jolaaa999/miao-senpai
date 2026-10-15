"""核心运行时：事件总线 + 路由 + 命令 + 插件 + 会话 + 服务。

    from ..core import EventBus, Router, Event, EventTypes
    from ..core import get_runtime, Runtime
    from ..core.commands import CommandRegistry, get_registry
    from ..core.plugins import Plugin, PluginRuntime
    from ..core.session import get_executor

Phase 3：Router 已正式接管真实 QQ 消息调度（原先由 NoneBot matcher 决定）。
"""

from __future__ import annotations

from .agent_trigger import AgentDecision, AgentTrigger, AgentTriggerPolicy
from .event_bus import (
    EventBus,
    EventFilter,
    EventHandler,
    Subscription,
    publish,
    publish_config_changed,
)
from .events import Event, EventTypes, Scope
from .router import (
    AGENT_PRIORITY,
    COMMAND_PRIORITY,
    PLUGIN_PRIORITY,
    STAGE_ORDER,
    Observer,
    Route,
    RouteContext,
    RouteHandler,
    RouteResult,
    RouteStage,
    Router,
)
from .runtime import Runtime, get_runtime, reset_runtime
from .services import ServiceRegistry, get_services

__all__ = [
    # 事件
    "Event",
    "EventTypes",
    "Scope",
    "EventBus",
    "EventHandler",
    "EventFilter",
    "Subscription",
    "publish",
    "publish_config_changed",
    # 路由
    "Route",
    "RouteContext",
    "RouteHandler",
    "RouteResult",
    "RouteStage",
    "Router",
    "Observer",
    "STAGE_ORDER",
    "COMMAND_PRIORITY",
    "PLUGIN_PRIORITY",
    "AGENT_PRIORITY",
    # Agent 触发
    "AgentTrigger",
    "AgentDecision",
    "AgentTriggerPolicy",
    # 服务
    "ServiceRegistry",
    "get_services",
    # 运行时
    "Runtime",
    "get_runtime",
    "reset_runtime",
]
