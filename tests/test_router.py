"""Router 测试：优先级、consume 短路、Command > Plugin > Agent 顺序。

Phase 2 只验证接口与调度语义；真实 QQ 消息仍由 NoneBot matcher 处理，
本 Router 不接入生产链路。
"""

from __future__ import annotations

import pytest

from dl_senpai.core import (
    AGENT_PRIORITY,
    COMMAND_PRIORITY,
    PLUGIN_PRIORITY,
    RouteContext,
    RouteResult,
    Router,
)


def ctx(**kw) -> RouteContext:
    base = {"text": "签到", "session_id": "group:1", "user_id": "9"}
    base.update(kw)
    return RouteContext(**base)


def always(_ctx: RouteContext) -> bool:
    return True


def never(_ctx: RouteContext) -> bool:
    return False


# ---------------------------------------------------------------- 基本匹配


@pytest.mark.asyncio
async def test_no_route_matched() -> None:
    router = Router()
    result = await router.dispatch(ctx())
    assert result.matched is False
    assert result.reason == "no_route_matched"


@pytest.mark.asyncio
async def test_command_route_matches() -> None:
    router = Router()
    router.command(
        "checkin",
        lambda c: c.text == "签到",
        lambda c: RouteResult.handle(reason="checkin"),
    )
    result = await router.dispatch(ctx(text="签到"))
    assert result.matched is True
    assert result.consumed is True
    assert result.stage == "command"


@pytest.mark.asyncio
async def test_matcher_false_skips_route() -> None:
    router = Router()
    router.command("checkin", never, lambda c: RouteResult.handle())
    result = await router.dispatch(ctx())
    assert result.matched is False


# ---------------------------------------------------------------- 三级顺序


@pytest.mark.asyncio
async def test_command_runs_before_plugin_and_agent() -> None:
    order: list[str] = []
    router = Router()
    router.agent_route("ai", always, lambda c: (order.append("agent"), RouteResult.handle())[1])
    router.plugin_route("plug", always, lambda c: (order.append("plugin"), RouteResult.handle())[1])
    router.command("cmd", always, lambda c: (order.append("command"), RouteResult.handle())[1])

    await router.dispatch(ctx())
    assert order == ["command"]


@pytest.mark.asyncio
async def test_plugin_runs_before_agent_when_no_command() -> None:
    order: list[str] = []
    router = Router()
    router.agent_route("ai", always, lambda c: (order.append("agent"), RouteResult.handle())[1])
    router.plugin_route("plug", always, lambda c: (order.append("plugin"), RouteResult.handle())[1])

    await router.dispatch(ctx())
    assert order == ["plugin"]


@pytest.mark.asyncio
async def test_agent_runs_when_nothing_else_matches() -> None:
    order: list[str] = []
    router = Router()
    router.plugin_route("plug", never, lambda c: RouteResult.handle())
    router.agent_route("ai", always, lambda c: (order.append("agent"), RouteResult.handle())[1])

    result = await router.dispatch(ctx())
    assert order == ["agent"]
    assert result.stage == "agent"


# ---------------------------------------------------------------- consume


@pytest.mark.asyncio
async def test_consume_stops_lower_stages() -> None:
    """插件 consume=True 时，Agent 不应再跑。"""
    order: list[str] = []
    router = Router()
    router.plugin_route(
        "plug", always, lambda c: (order.append("plugin"), RouteResult.handle(consumed=True))[1]
    )
    router.agent_route("ai", always, lambda c: (order.append("agent"), RouteResult.handle())[1])

    await router.dispatch(ctx())
    assert order == ["plugin"]


@pytest.mark.asyncio
async def test_non_consume_falls_through() -> None:
    """插件 matched 但 consumed=False 时，Agent 仍应执行。

    这正是贴纸收集器的语义：它处理了消息，但不该阻止 AI 回复。
    """
    order: list[str] = []
    router = Router()
    router.plugin_route(
        "collect",
        always,
        lambda c: (order.append("collect"), RouteResult.handle(consumed=False))[1],
    )
    router.agent_route("ai", always, lambda c: (order.append("agent"), RouteResult.handle())[1])

    result = await router.dispatch(ctx())
    assert order == ["collect", "agent"]
    assert result.stage == "agent"


@pytest.mark.asyncio
async def test_multiple_non_consuming_plugins_all_run() -> None:
    order: list[str] = []
    router = Router()
    for name in ("a", "b", "c"):
        router.plugin_route(
            name,
            always,
            (lambda n: lambda c: (order.append(n), RouteResult.handle(consumed=False))[1])(name),
            priority=PLUGIN_PRIORITY,
        )
    await router.dispatch(ctx())
    assert order == ["a", "b", "c"]


# ---------------------------------------------------------------- priority


@pytest.mark.asyncio
async def test_lower_priority_number_runs_first() -> None:
    order: list[str] = []
    router = Router()
    router.plugin_route(
        "high", always, lambda c: (order.append("high"), RouteResult.handle(consumed=False))[1],
        priority=10,
    )
    router.plugin_route(
        "low", always, lambda c: (order.append("low"), RouteResult.handle(consumed=False))[1],
        priority=20,
    )
    await router.dispatch(ctx())
    assert order == ["high", "low"]


@pytest.mark.asyncio
async def test_priority_registration_order_independent() -> None:
    """先注册低优先级、后注册高优先级，执行顺序仍按 priority。"""
    order: list[str] = []
    router = Router()
    router.plugin_route(
        "low", always, lambda c: (order.append("low"), RouteResult.handle(consumed=False))[1],
        priority=99,
    )
    router.plugin_route(
        "high", always, lambda c: (order.append("high"), RouteResult.handle(consumed=False))[1],
        priority=1,
    )
    await router.dispatch(ctx())
    assert order == ["high", "low"]


def test_default_priorities_are_ordered() -> None:
    """默认优先级必须体现 Command > Plugin > Agent。"""
    assert COMMAND_PRIORITY < PLUGIN_PRIORITY < AGENT_PRIORITY


# ---------------------------------------------------------------- 结果字段


def test_route_result_helpers() -> None:
    skip = RouteResult.skip(reason="nope")
    assert skip.matched is False and skip.consumed is False
    handled = RouteResult.handle(reason="ok")
    assert handled.matched is True and handled.consumed is True
    loose = RouteResult.handle(consumed=False)
    assert loose.matched is True and loose.consumed is False


def test_route_result_log_fields() -> None:
    fields = RouteResult.handle(reason="checkin").as_log_fields()
    assert fields["matched"] is True
    assert fields["reason"] == "checkin"


# ---------------------------------------------------------------- 生命周期


def test_unregister_by_name() -> None:
    router = Router()
    router.command("c", always, lambda c: RouteResult.handle())
    assert router.unregister("c") == 1
    assert router.routes("command") == []


def test_unregister_respects_owner() -> None:
    """插件 A 不能卸载插件 B 的同名路由。"""
    router = Router()
    router.plugin_route("dup", always, lambda c: RouteResult.handle(), owner="a")
    router.plugin_route("dup", always, lambda c: RouteResult.handle(), owner="b")
    assert router.unregister("dup", owner="a") == 1
    remaining = router.routes("plugin")
    assert len(remaining) == 1
    assert remaining[0].owner == "b"


def test_unregister_owner_bulk() -> None:
    router = Router()
    router.plugin_route("x", always, lambda c: RouteResult.handle(), owner="p")
    router.command("y", always, lambda c: RouteResult.handle(), owner="p")
    router.plugin_route("z", always, lambda c: RouteResult.handle(), owner="other")
    assert router.unregister_owner("p") == 2
    assert len(router.routes()) == 1


def test_reregister_same_name_owner_replaces() -> None:
    """同名同 owner 重注册 = 替换（防 reload 重复注册）。"""
    router = Router()
    router.plugin_route("p", always, lambda c: RouteResult.handle(), owner="plug")
    router.plugin_route("p", always, lambda c: RouteResult.handle(), owner="plug")
    assert len(router.routes("plugin")) == 1


def test_register_unknown_stage_raises() -> None:
    from dl_senpai.core import Route

    router = Router()
    with pytest.raises(ValueError):
        router.register(Route(name="bad", stage="nope", matcher=always, handler=lambda c: None))  # type: ignore[arg-type]


def test_routes_sorted_by_stage() -> None:
    router = Router()
    router.agent_route("a", always, lambda c: RouteResult.handle())
    router.command("c", always, lambda c: RouteResult.handle())
    router.plugin_route("p", always, lambda c: RouteResult.handle())
    assert [r.stage for r in router.routes()] == ["command", "plugin", "agent"]


# ---------------------------------------------------------------- 健壮性


@pytest.mark.asyncio
async def test_handler_exception_does_not_crash_dispatch() -> None:
    def boom(_c: RouteContext) -> RouteResult:
        raise RuntimeError("handler boom")

    router = Router()
    router.plugin_route("bad", always, boom)
    router.agent_route("ai", always, lambda c: RouteResult.handle(reason="fallback"))
    result = await router.dispatch(ctx())
    # 坏 handler 被跳过，Agent 仍然兜底
    assert result.stage == "agent"
    assert result.reason == "fallback"


@pytest.mark.asyncio
async def test_matcher_exception_means_no_match() -> None:
    def bad_matcher(_c: RouteContext) -> bool:
        raise RuntimeError("matcher boom")

    router = Router()
    router.command("bad", bad_matcher, lambda c: RouteResult.handle())
    result = await router.dispatch(ctx())
    assert result.matched is False


@pytest.mark.asyncio
async def test_async_handler_supported() -> None:
    async def handler(_c: RouteContext) -> RouteResult:
        return RouteResult.handle(reason="async")

    router = Router()
    router.command("a", always, handler)
    result = await router.dispatch(ctx())
    assert result.reason == "async"


@pytest.mark.asyncio
async def test_non_route_result_return_is_tolerated() -> None:
    """handler 返回 None 时按"已处理且短路"处理，不崩溃。"""
    router = Router()
    router.command("loose", always, lambda c: None)  # type: ignore[arg-type,return-value]
    result = await router.dispatch(ctx())
    assert result.matched is True
    assert result.reason == "implicit"


@pytest.mark.asyncio
async def test_dispatch_emits_events() -> None:
    from dl_senpai.core import EventBus

    bus = EventBus()
    seen: list[str] = []
    bus.subscribe("route.*", lambda e: seen.append(e.type))
    router = Router(bus=bus)
    router.command("c", always, lambda c: RouteResult.handle())
    await router.dispatch(ctx())
    assert "route.matched" in seen


@pytest.mark.asyncio
async def test_route_context_carries_session_info() -> None:
    captured: list[RouteContext] = []

    def handler(c: RouteContext) -> RouteResult:
        captured.append(c)
        return RouteResult.handle()

    router = Router()
    router.command("c", always, handler)
    await router.dispatch(ctx(session_id="group:42", user_id="7", is_tome=True))
    assert captured[0].session_id == "group:42"
    assert captured[0].user_id == "7"
    assert captured[0].is_tome is True
