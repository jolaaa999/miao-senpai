"""事件总线测试：订阅、发布、取消、命名空间过滤、异常隔离。"""

from __future__ import annotations

import asyncio

import pytest

from dl_senpai.core import Event, EventBus, EventTypes, publish


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


# ---------------------------------------------------------------- subscribe / publish


def test_exact_subscribe_and_publish(bus: EventBus) -> None:
    seen: list[Event] = []
    bus.subscribe(EventTypes.MESSAGE_RECEIVED, seen.append)
    delivered = bus.publish(Event(type=EventTypes.MESSAGE_RECEIVED))
    assert delivered == 1
    assert len(seen) == 1
    assert seen[0].type == "message.received"


def test_publish_to_no_subscriber_is_noop(bus: EventBus) -> None:
    assert bus.publish(Event(type="nobody.listens")) == 0


def test_publish_returns_subscriber_count(bus: EventBus) -> None:
    bus.subscribe("a.b", lambda e: None)
    bus.subscribe("a.b", lambda e: None)
    assert bus.publish(Event(type="a.b")) == 2


def test_events_carry_payload(bus: EventBus) -> None:
    captured: list[Event] = []
    bus.subscribe("tool.finished", captured.append)
    publish(bus, "tool.finished", source="agent", tool="search", ok=True)
    assert captured[0].get("tool") == "search"
    assert captured[0].get("ok") is True
    assert captured[0].source == "agent"


# ---------------------------------------------------------------- unsubscribe


def test_unsubscribe_stops_delivery(bus: EventBus) -> None:
    seen: list[Event] = []
    sub = bus.subscribe("x.y", seen.append)
    bus.publish(Event(type="x.y"))
    assert bus.unsubscribe(sub) is True
    bus.publish(Event(type="x.y"))
    assert len(seen) == 1


def test_unsubscribe_twice_returns_false(bus: EventBus) -> None:
    sub = bus.subscribe("x.y", lambda e: None)
    assert bus.unsubscribe(sub) is True
    assert bus.unsubscribe(sub) is False
    assert bus.unsubscribe(None) is False


def test_unsubscribe_owner_removes_all(bus: EventBus) -> None:
    """插件 disable 时按 owner 批量清理（Phase 4 基础）。"""
    bus.subscribe("a.b", lambda e: None, owner="plugin_a")
    bus.subscribe("c.d", lambda e: None, owner="plugin_a")
    bus.subscribe("a.b", lambda e: None, owner="plugin_b")
    removed = bus.unsubscribe_owner("plugin_a")
    assert removed == 2
    assert bus.subscriber_count() == 1


def test_unsubscribe_owner_unknown_returns_zero(bus: EventBus) -> None:
    assert bus.unsubscribe_owner("nobody") == 0
    assert bus.unsubscribe_owner("") == 0


def test_reload_does_not_duplicate_registration(bus: EventBus) -> None:
    """同一 owner 重复注册后再按 owner 清理，不应留下残留。"""
    for _ in range(3):
        bus.unsubscribe_owner("p")
        bus.subscribe("a.b", lambda e: None, owner="p")
    assert bus.subscriber_count() == 1


# ---------------------------------------------------------------- 命名空间过滤


def test_domain_wildcard_matches(bus: EventBus) -> None:
    seen: list[str] = []
    bus.subscribe("message.*", lambda e: seen.append(e.type))
    bus.publish(Event(type="message.received"))
    bus.publish(Event(type="message.sent"))
    bus.publish(Event(type="tool.finished"))
    assert seen == ["message.received", "message.sent"]


def test_star_matches_everything(bus: EventBus) -> None:
    seen: list[str] = []
    bus.subscribe("*", lambda e: seen.append(e.type))
    bus.publish(Event(type="a.b"))
    bus.publish(Event(type="c.d"))
    assert seen == ["a.b", "c.d"]


def test_exact_outranks_wildcard(bus: EventBus) -> None:
    """精确订阅先于通配订阅收到事件（越具体越先）。"""
    order: list[str] = []
    bus.subscribe("*", lambda e: order.append("star"))
    bus.subscribe("message.*", lambda e: order.append("domain"))
    bus.subscribe("message.received", lambda e: order.append("exact"))
    bus.publish(Event(type="message.received"))
    assert order == ["exact", "domain", "star"]


def test_wildcard_does_not_leak_to_other_domain(bus: EventBus) -> None:
    seen: list[str] = []
    bus.subscribe("message.*", lambda e: seen.append(e.type))
    bus.publish(Event(type="messages.received"))  # 注意复数，不应匹配
    assert seen == []


def test_condition_filter(bus: EventBus) -> None:
    seen: list[str] = []
    bus.subscribe(
        "tool.finished",
        lambda e: seen.append(e.get("tool")),
        where=lambda e: e.get("ok") is False,
    )
    publish(bus, "tool.finished", tool="search", ok=True)
    publish(bus, "tool.finished", tool="draw", ok=False)
    assert seen == ["draw"]


def test_filter_exception_means_no_match(bus: EventBus) -> None:
    def bad_filter(event: Event) -> bool:
        raise RuntimeError("filter boom")

    seen: list[str] = []
    bus.subscribe("a.b", lambda e: seen.append("hit"), where=bad_filter)
    bus.publish(Event(type="a.b"))  # 不应抛异常
    assert seen == []


# ---------------------------------------------------------------- 异常隔离


def test_handler_exception_isolated(bus: EventBus) -> None:
    """一个订阅者抛异常，不能影响其他订阅者。"""
    seen: list[str] = []

    def boom(event: Event) -> None:
        raise RuntimeError("handler boom")

    bus.subscribe("a.b", boom)
    bus.subscribe("a.b", lambda e: seen.append("ok"))
    delivered = bus.publish(Event(type="a.b"))  # 不应抛
    assert delivered == 2
    assert seen == ["ok"]


def test_publish_never_raises_from_handler(bus: EventBus) -> None:
    bus.subscribe("a.b", lambda e: 1 / 0)
    assert bus.publish(Event(type="a.b")) == 1


def test_once_subscription_fires_once(bus: EventBus) -> None:
    seen: list[str] = []
    bus.subscribe("a.b", lambda e: seen.append("x"), once=True)
    bus.publish(Event(type="a.b"))
    bus.publish(Event(type="a.b"))
    assert seen == ["x"]


# ---------------------------------------------------------------- async


@pytest.mark.asyncio
async def test_async_handler_scheduled(bus: EventBus) -> None:
    seen: list[str] = []

    async def handler(event: Event) -> None:
        seen.append(event.type)

    bus.subscribe("a.b", handler)
    bus.publish(Event(type="a.b"))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert seen == ["a.b"]


@pytest.mark.asyncio
async def test_publish_async_awaits_handlers(bus: EventBus) -> None:
    seen: list[str] = []

    async def handler(event: Event) -> None:
        await asyncio.sleep(0)
        seen.append("done")

    bus.subscribe("a.b", handler)
    delivered = await bus.publish_async(Event(type="a.b"))
    assert delivered == 1
    assert seen == ["done"]


@pytest.mark.asyncio
async def test_publish_async_isolates_exceptions(bus: EventBus) -> None:
    seen: list[str] = []

    async def boom(event: Event) -> None:
        raise RuntimeError("async boom")

    bus.subscribe("a.b", boom)
    bus.subscribe("a.b", lambda e: seen.append("ok"))
    await bus.publish_async(Event(type="a.b"))
    assert seen == ["ok"]


# ---------------------------------------------------------------- Event 模型


def test_event_auto_fills_id_and_time() -> None:
    e = Event(type="a.b")
    assert e.id
    assert e.time > 0


def test_event_ids_are_unique() -> None:
    ids = {Event(type="a.b").id for _ in range(200)}
    assert len(ids) == 200


def test_event_domain_and_action() -> None:
    e = Event(type="message.received")
    assert e.domain == "message"
    assert e.action == "received"


def test_event_inherits_trace_id_from_context() -> None:
    from dl_senpai.observability import trace_scope

    with trace_scope(session_id="group:1"):
        e = Event(type="a.b")
    assert e.trace_id != ""


def test_event_scope_defaults_to_global() -> None:
    assert Event(type="a.b").scope == "global"
