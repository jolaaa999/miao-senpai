"""统一事件模型。

设计边界（务必遵守，否则会重蹈「万物皆事件」的覆辙）：

EventBus 只负责**跨模块通信**。以下场景**不要**发事件：
- 一个模块内部的两个函数互相调用；
- 单纯读一个变量、查一次数据库；
- 需要返回值、需要确定性顺序的调用。

判断标准：如果事件没有订阅者时程序行为应当完全不变，那它才适合做事件。

事件名格式：``domain.action``，action 用**已经发生的事实**（过去式），
例如 ``message.received`` 而不是 ``receive_message``。这样日志读起来是
「发生了什么」，而不是「即将做什么」。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

# 事件作用域：决定事件的传播范围与可见性
Scope = Literal["global", "group", "user", "private", "session"]


@dataclass(frozen=True)
class Event:
    """一条事件。

    使用 frozen dataclass 而非 pydantic：
    1. 事件是**不可变事实**，发出后不应被订阅者改写；
    2. 零额外依赖，构造开销小（每条 QQ 消息会发多个事件）；
    3. payload 里的领域对象往往不是 pydantic model，避免强制转换。
    """

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    time: float = 0.0
    source: str = ""
    actor: str = ""
    trace_id: str = ""
    scope: Scope = "global"

    def __post_init__(self) -> None:
        # frozen dataclass 需要 object.__setattr__ 来补默认值
        if not self.id:
            object.__setattr__(self, "id", uuid.uuid4().hex[:16])
        if not self.time:
            object.__setattr__(self, "time", time.time())
        if not self.trace_id:
            # 延迟 import 避免 core ↔ observability 循环依赖
            try:
                from ..observability.context import trace_id as _trace_id

                tid = _trace_id()
            except Exception:  # noqa: BLE001
                tid = ""
            if tid:
                object.__setattr__(self, "trace_id", tid)

    # -- 便捷构造 --

    @property
    def domain(self) -> str:
        """``message.received`` → ``message``"""
        return self.type.split(".", 1)[0] if "." in self.type else self.type

    @property
    def action(self) -> str:
        """``message.received`` → ``received``"""
        return self.type.split(".", 1)[1] if "." in self.type else ""

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)

    def as_log_fields(self) -> dict[str, Any]:
        """转成日志字段。不展开整个 payload（可能含大对象）。"""
        return {
            "event": self.type,
            "event_id": self.id,
            "source": self.source,
            "actor": self.actor,
            "scope": self.scope,
            "trace_id": self.trace_id,
        }


class EventTypes:
    """事件名常量。

    集中定义便于 IDE 跳转与拼写检查；同时作为「本项目当前有哪些事件」的
    文档。新增事件时在此登记。
    """

    # 消息域
    MESSAGE_RECEIVED = "message.received"
    MESSAGE_SENT = "message.sent"

    # 路由/命令域
    ROUTE_MATCHED = "route.matched"
    ROUTE_SKIPPED = "route.skipped"
    COMMAND_INVOKED = "command.invoked"

    # Agent / 工具域
    AGENT_STARTED = "agent.started"
    AGENT_FINISHED = "agent.finished"
    TOOL_STARTED = "tool.started"
    TOOL_FINISHED = "tool.finished"
    TOOL_FAILED = "tool.failed"

    # 插件域
    PLUGIN_LOADED = "plugin.loaded"
    PLUGIN_ENABLED = "plugin.enabled"
    PLUGIN_DISABLED = "plugin.disabled"
    PLUGIN_RELOADED = "plugin.reloaded"

    # 配置域
    CONFIG_CHANGED = "config.changed"


__all__ = ["Event", "EventTypes", "Scope"]
