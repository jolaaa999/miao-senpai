"""事件总线：跨模块通信的唯一下行通道。

支持的订阅模式（刻意保持简单，不做 DSL）：

    bus.subscribe("message.received", handler)   # 精确匹配
    bus.subscribe("message.*", handler)          # 域通配
    bus.subscribe("*", handler)                  # 全部（仅用于日志/调试）

以及可选的条件过滤：

    bus.subscribe("tool.finished", handler, where=lambda e: e.get("ok") is False)

异常隔离是硬要求：一个订阅者抛异常**绝不能**影响其他订阅者，也不能让
`publish()` 的调用方（通常是消息处理主链路）失败。异常会被记录到日志与
指标，但不会向上传播——否则「加一个统计插件」就能把机器人搞挂。
"""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from .events import Event, EventTypes

# 订阅者可以是同步函数或协程函数
EventHandler = Callable[[Event], Any] | Callable[[Event], Awaitable[Any]]
EventFilter = Callable[[Event], bool]

# 通配订阅的优先级低于精确订阅，保证「越具体越先收到」
_PRIORITY_EXACT = 0
_PRIORITY_DOMAIN = 10
_PRIORITY_ALL = 20


@dataclass
class Subscription:
    """一次订阅。handlers 通过返回的 token 取消订阅。"""

    pattern: str
    handler: EventHandler
    where: EventFilter | None = None
    priority: int = _PRIORITY_EXACT
    order: int = 0
    owner: str = ""
    once: bool = False
    _active: bool = True

    def matches(self, event: Event) -> bool:
        if not self._active:
            return False
        if not _pattern_matches(self.pattern, event.type):
            return False
        if self.where is not None:
            try:
                if not self.where(event):
                    return False
            except Exception:  # noqa: BLE001 — 过滤条件异常视为不匹配
                return False
        return True


def _pattern_matches(pattern: str, event_type: str) -> bool:
    """`*` 匹配全部；`message.*` 匹配该域下任意 action；其余精确匹配。"""
    if pattern == "*":
        return True
    if pattern.endswith(".*"):
        return event_type.startswith(pattern[:-1])  # 保留末尾的点
    return pattern == event_type


class EventBus:
    """进程内事件总线。

    不引入 asyncio.Queue 是有意为之：本项目的事件都是「发完即忘」的
    通知型事件，同步分发可以让发布顺序确定、便于测试。异步 handler 会被
    调度成 task，不阻塞发布方。
    """

    def __init__(self, *, logger_name: str = "dl_senpai.event_bus") -> None:
        self._subs: list[Subscription] = []
        self._order = 0
        self._logger_name = logger_name

    # -- 订阅 --

    def subscribe(
        self,
        pattern: str,
        handler: EventHandler,
        *,
        where: EventFilter | None = None,
        owner: str = "",
        once: bool = False,
    ) -> Subscription:
        """注册订阅。

        Args:
            pattern: 事件名或通配模式（``message.*`` / ``*``）。
            handler: 同步或协程函数，接收 ``Event``。
            where: 可选条件过滤，返回 False 则跳过该订阅者。
            owner: 订阅归属者（插件名），便于 ``unsubscribe_owner()`` 批量清理
                ——这是 Phase 4 插件 disable 时「清理所有 hook」的基础。
            once: 只触发一次后自动退订。
        """
        if pattern == "*":
            priority = _PRIORITY_ALL
        elif pattern.endswith(".*"):
            priority = _PRIORITY_DOMAIN
        else:
            priority = _PRIORITY_EXACT
        self._order += 1
        sub = Subscription(
            pattern=pattern,
            handler=handler,
            where=where,
            priority=priority,
            order=self._order,
            owner=owner,
            once=once,
        )
        self._subs.append(sub)
        # 保证「精确 > 域通配 > 全量」，同优先级按注册顺序
        self._subs.sort(key=lambda s: (s.priority, s.order))
        return sub

    def unsubscribe(self, sub: Subscription | None) -> bool:
        """取消一个订阅；返回是否真的取消了。"""
        if sub is None:
            return False
        if not sub._active:
            return False
        sub._active = False
        try:
            self._subs.remove(sub)
        except ValueError:
            return False
        return True

    def unsubscribe_owner(self, owner: str) -> int:
        """按归属者批量取消订阅（插件 disable / reload 用）。

        返回取消数量。这是防止「reload 导致重复注册」的关键机制。
        """
        if not owner:
            return 0
        removed = [s for s in self._subs if s.owner == owner]
        for sub in removed:
            sub._active = False
        self._subs = [s for s in self._subs if s.owner != owner]
        return len(removed)

    def subscriber_count(self, pattern: str | None = None) -> int:
        if pattern is None:
            return len(self._subs)
        return sum(1 for s in self._subs if s.pattern == pattern)

    # -- 发布 --

    def publish(self, event: Event) -> int:
        """同步分发事件给所有匹配订阅者；返回触达的订阅者数量。

        同步 handler 立即执行；协程 handler 被调度成 task。任何异常都被
        隔离并记录，不会传播给调用方。
        """
        delivered = 0
        for sub in list(self._subs):
            if not sub.matches(event):
                continue
            delivered += 1
            if sub.once:
                self.unsubscribe(sub)
            self._invoke(sub, event)
        return delivered

    async def publish_async(self, event: Event) -> int:
        """异步分发：等待所有订阅者（含协程）执行完毕。

        用于需要「事件处理完再继续」的场景。同样做异常隔离。
        """
        delivered = 0
        for sub in list(self._subs):
            if not sub.matches(event):
                continue
            delivered += 1
            if sub.once:
                self.unsubscribe(sub)
            result = self._invoke(sub, event, schedule=False)
            if inspect.isawaitable(result):
                try:
                    await result
                except Exception as exc:  # noqa: BLE001
                    self._report_error(sub, event, exc)
        return delivered

    # -- 内部 --

    def _invoke(self, sub: Subscription, event: Event, *, schedule: bool = True) -> Any:
        try:
            result = sub.handler(event)
        except Exception as exc:  # noqa: BLE001 — 订阅者异常必须隔离
            self._report_error(sub, event, exc)
            return None
        if inspect.isawaitable(result):
            if schedule:
                # 放到事件循环上跑；没有运行中的 loop（同步测试环境）时
                # 退化为直接丢弃，并记录——避免测试里出现
                # "coroutine was never awaited" 警告
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    try:
                        result.close()  # type: ignore[union-attr]
                    except Exception:  # noqa: BLE001
                        pass
                    return None
                task = loop.create_task(result)

                def _on_done(t: asyncio.Task) -> None:
                    exc = t.exception()
                    if exc is not None:
                        self._report_error(sub, event, exc)

                task.add_done_callback(_on_done)
            else:
                return result
        return None

    def _report_error(self, sub: Subscription, event: Event, exc: BaseException) -> None:
        try:
            from ..observability.logging import log_event

            log_event(
                "event_bus.handler_failed",
                level="warning",
                pattern=sub.pattern,
                owner=sub.owner or None,
                event_type=event.type,
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001 — 日志失败也不能影响
            pass
        try:
            from ..observability.metrics import metrics

            metrics.incr("event_bus.handler_failed")
        except Exception:  # noqa: BLE001
            pass


# -- 便捷发布函数 --

def publish(
    bus: EventBus,
    event_type: str,
    *,
    source: str = "",
    actor: str = "",
    scope: Any = "global",
    **payload: Any,
) -> int:
    """构造并发布事件的一站式入口。

    trace_id 由 Event 自动从当前上下文继承，调用方通常不需要传。
    """
    return bus.publish(
        Event(
            type=event_type,
            payload=payload,
            source=source,
            actor=str(actor) if actor else "",
            scope=scope,
        )
    )


def publish_config_changed(bus: EventBus, old_version: int, new_version: int) -> int:
    """发布配置变更事件（config.changed）。"""
    return publish(
        bus,
        EventTypes.CONFIG_CHANGED,
        source="config",
        old_version=old_version,
        new_version=new_version,
    )


__all__ = [
    "EventBus",
    "EventHandler",
    "EventFilter",
    "Subscription",
    "publish",
    "publish_config_changed",
]
