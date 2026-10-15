"""第一层测试：Command Registry 与 Plugin Runtime 单元测试。"""

from __future__ import annotations

import pytest

from dl_senpai.core.commands.models import Command
from dl_senpai.core.commands.registry import CommandRegistry
from dl_senpai.core.event_bus import EventBus
from dl_senpai.core.plugins.models import Plugin
from dl_senpai.core.plugins.runtime import PluginRuntime
from dl_senpai.core.router import Route, RouteResult, Router
from dl_senpai.core.services import ServiceRegistry


def make_registry() -> CommandRegistry:
    return CommandRegistry()


async def _noop(ctx, match):
    return None


# ================================================================
# Command Registry
# ================================================================


def test_register_and_match() -> None:
    reg = make_registry()
    reg.register(Command(name="a", patterns=[("go", r"^走$")], handler=_noop))
    inv = reg.match("走")
    assert inv is not None
    assert inv.name == "a"
    assert inv.match.action == "go"


def test_match_returns_none_for_unknown() -> None:
    reg = make_registry()
    reg.register(Command(name="a", patterns=[r"^走$"], handler=_noop))
    assert reg.match("跑") is None


def test_match_empty_text() -> None:
    reg = make_registry()
    reg.register(Command(name="a", patterns=[r"^走$"], handler=_noop))
    assert reg.match("") is None
    assert reg.match("   ") is None


def test_priority_ordering() -> None:
    reg = make_registry()
    reg.register(Command(name="low", patterns=[r"^x$"], handler=_noop, priority=200))
    reg.register(Command(name="high", patterns=[r"^x$"], handler=_noop, priority=10))
    inv = reg.match("x")
    assert inv is not None and inv.name == "high"


def test_registration_order_does_not_affect_priority() -> None:
    reg = make_registry()
    reg.register(Command(name="high", patterns=[r"^x$"], handler=_noop, priority=1))
    reg.register(Command(name="low", patterns=[r"^x$"], handler=_noop, priority=99))
    assert reg.match("x").name == "high"


def test_multi_pattern_action_mapping() -> None:
    reg = make_registry()
    reg.register(
        Command(
            name="shop",
            patterns=[("open", r"^商店$"), ("help", r"^帮助$")],
            handler=_noop,
        )
    )
    assert reg.match("商店").match.action == "open"
    assert reg.match("帮助").match.action == "help"


def test_pattern_capture_groups() -> None:
    reg = make_registry()
    reg.register(Command(name="buy", patterns=[("buy", r"^买(.+)$")], handler=_noop))
    inv = reg.match("买裙子")
    assert inv.match.arg(0) == "裙子"


def test_unregister() -> None:
    reg = make_registry()
    reg.register(Command(name="a", patterns=[r"^x$"], handler=_noop))
    assert reg.unregister("a") is True
    assert reg.unregister("a") is False
    assert reg.match("x") is None


def test_unregister_respects_owner() -> None:
    reg = make_registry()
    reg.register(Command(name="a", patterns=[r"^x$"], handler=_noop, owner="p1"))
    assert reg.unregister("a", owner="p2") is False
    assert reg.unregister("a", owner="p1") is True


def test_unregister_owner_bulk() -> None:
    reg = make_registry()
    reg.register(Command(name="a", patterns=[r"^x$"], handler=_noop, owner="p1"))
    reg.register(Command(name="b", patterns=[r"^y$"], handler=_noop, owner="p1"))
    reg.register(Command(name="c", patterns=[r"^z$"], handler=_noop, owner="p2"))
    assert reg.unregister_owner("p1") == 2
    assert len(reg) == 1


def test_enable_disable() -> None:
    reg = make_registry()
    reg.register(Command(name="a", patterns=[r"^x$"], handler=_noop))
    assert reg.disable("a") is True
    assert reg.match("x") is None
    assert reg.enable("a") is True
    assert reg.match("x") is not None


def test_enable_unknown_returns_false() -> None:
    reg = make_registry()
    assert reg.enable("nope") is False
    assert reg.disable("nope") is False


def test_set_owner_enabled_bulk() -> None:
    reg = make_registry()
    reg.register(Command(name="a", patterns=[r"^x$"], handler=_noop, owner="p"))
    reg.register(Command(name="b", patterns=[r"^y$"], handler=_noop, owner="p"))
    assert reg.set_owner_enabled("p", False) == 2
    assert reg.match("x") is None


def test_list_and_namespaces() -> None:
    reg = make_registry()
    reg.register(Command(name="a", patterns=[r"^x$"], handler=_noop, namespace="ns1"))
    reg.register(Command(name="b", patterns=[r"^y$"], handler=_noop, namespace="ns2"))
    assert [c.name for c in reg.list()] == ["a", "b"]
    assert [c.name for c in reg.list(namespace="ns2")] == ["b"]
    assert reg.namespaces() == ["ns1", "ns2"]


def test_duplicate_register_replaces() -> None:
    """同名重注册 = 替换（防 reload 重复）。"""
    reg = make_registry()
    reg.register(Command(name="a", patterns=[r"^x$"], handler=_noop, priority=50))
    reg.register(Command(name="a", patterns=[r"^y$"], handler=_noop, priority=10))
    assert len(reg) == 1
    assert reg.match("y") is not None
    assert reg.match("x") is None


def test_guard_filters_match() -> None:
    reg = make_registry()
    reg.register(
        Command(name="a", patterns=[r"^x$"], handler=_noop, guard=lambda ctx: False)
    )
    assert reg.match("x", ctx=object()) is None


def test_guard_exception_means_no_match() -> None:
    def boom(ctx):
        raise RuntimeError("guard boom")

    reg = make_registry()
    reg.register(Command(name="a", patterns=[r"^x$"], handler=_noop, guard=boom))
    assert reg.match("x", ctx=object()) is None


def test_guard_falls_through_to_next_command() -> None:
    """guard 不通过时应继续尝试后续命令，而不是直接放弃。"""
    reg = make_registry()
    reg.register(
        Command(name="first", patterns=[r"^x$"], handler=_noop, priority=1,
                guard=lambda ctx: False)
    )
    reg.register(Command(name="second", patterns=[r"^x$"], handler=_noop, priority=2))
    assert reg.match("x", ctx=object()).name == "second"


def test_compiled_patterns_cached() -> None:
    """正则必须在注册时编译一次，不在每条消息上重复编译。"""
    cmd = Command(name="a", patterns=[r"^x$"], handler=_noop)
    cmd.compile()
    first = cmd.compiled_patterns
    assert first is cmd.compiled_patterns


def test_string_patterns_accepted() -> None:
    reg = make_registry()
    reg.register(Command(name="a", patterns=[r"^x$"], handler=_noop))
    assert reg.match("x") is not None


def test_contains() -> None:
    reg = make_registry()
    reg.register(Command(name="a", patterns=[r"^x$"], handler=_noop))
    assert "a" in reg
    assert "b" not in reg


# ================================================================
# Plugin Runtime
# ================================================================


def make_runtime() -> tuple[PluginRuntime, CommandRegistry, Router, EventBus]:
    commands = CommandRegistry()
    bus = EventBus()
    router = Router(bus=bus)
    runtime = PluginRuntime(
        commands=commands, router=router, bus=bus, services=ServiceRegistry()
    )
    return runtime, commands, router, bus


def simple_plugin(pid: str = "p1") -> Plugin:
    return Plugin(
        id=pid,
        name=pid,
        commands=[Command(name=f"{pid}_cmd", patterns=[r"^x$"], handler=_noop)],
        routes=[
            Route(name=f"{pid}_route", stage="plugin", matcher=lambda c: False,
                  handler=lambda c: RouteResult.handle())
        ],
        subscriptions_spec=[("message.received", lambda e: None, None)],
    )


def test_load_and_enable() -> None:
    runtime, commands, router, bus = make_runtime()
    runtime.load(simple_plugin())
    assert "p1" in runtime
    assert runtime.enable("p1") is True
    assert "p1_cmd" in commands
    assert len(router.routes("plugin")) == 1
    assert bus.subscriber_count("message.received") == 1


def test_disable_cleans_everything() -> None:
    """disable 必须清理命令、路由、订阅 —— 这是核心验收条件。"""
    runtime, commands, router, bus = make_runtime()
    runtime.load(simple_plugin())
    runtime.enable("p1")
    runtime.disable("p1")
    assert "p1_cmd" not in commands
    assert router.routes("plugin") == []
    assert bus.subscriber_count("message.received") == 0


def test_enable_disable_enable_no_duplicates() -> None:
    """enable → disable → enable 之后，注册项不能翻倍。"""
    runtime, commands, router, bus = make_runtime()
    runtime.load(simple_plugin())
    for _ in range(3):
        runtime.enable("p1")
        runtime.disable("p1")
    runtime.enable("p1")
    assert len(commands) == 1
    assert len(router.routes("plugin")) == 1
    assert bus.subscriber_count("message.received") == 1


def test_reload_does_not_duplicate() -> None:
    """reload 任意次后，同一事件仍只触发一次。"""
    runtime, commands, router, bus = make_runtime()
    runtime.load(simple_plugin())
    runtime.enable("p1")
    for _ in range(5):
        runtime.reload("p1")
    assert len(commands) == 1
    assert len(router.routes("plugin")) == 1
    assert bus.subscriber_count("message.received") == 1


def test_reload_preserves_enabled_state() -> None:
    runtime, _, _, _ = make_runtime()
    runtime.load(simple_plugin())
    runtime.enable("p1")
    assert runtime.get("p1").active is True
    runtime.reload("p1")
    assert runtime.get("p1").active is True


def test_reload_disabled_plugin_stays_disabled() -> None:
    """disable → reload 后不应被重新激活（否则禁用形同虚设）。"""
    runtime, commands, _, _ = make_runtime()
    runtime.load(simple_plugin())
    runtime.enable("p1")
    runtime.disable("p1")
    assert runtime.get("p1").active is False
    runtime.reload("p1")
    assert runtime.get("p1").active is False
    assert len(commands) == 0, "reload 后禁用的插件不应注册命令"


def test_load_alone_does_not_activate() -> None:
    """load 只登记定义，不激活——`enabled`（意图）与 `active`（事实）必须可区分。"""
    runtime, commands, router, bus = make_runtime()
    runtime.load(simple_plugin())
    assert runtime.get("p1").active is False
    assert len(commands) == 0
    assert router.routes("plugin") == []
    assert bus.subscriber_count("message.received") == 0


def test_unload_removes_plugin() -> None:
    runtime, commands, _, _ = make_runtime()
    runtime.load(simple_plugin())
    runtime.enable("p1")
    assert runtime.unload("p1") is True
    assert "p1" not in runtime
    assert len(commands) == 0


def test_load_same_id_replaces() -> None:
    runtime, commands, _, _ = make_runtime()
    runtime.load(simple_plugin())
    runtime.enable("p1")
    runtime.load(simple_plugin())  # 再 load 一次
    runtime.enable("p1")
    assert len(commands) == 1


def test_enable_unknown_plugin() -> None:
    runtime, _, _, _ = make_runtime()
    assert runtime.enable("nope") is False
    assert runtime.disable("nope") is False
    assert runtime.reload("nope") is False
    assert runtime.unload("nope") is False


def test_plugin_owner_is_set_on_registration() -> None:
    runtime, commands, router, _ = make_runtime()
    runtime.load(simple_plugin("pid"))
    runtime.enable("pid")
    assert commands.get("pid_cmd").owner == "pid"
    assert router.routes("plugin")[0].owner == "pid"


def test_two_plugins_isolated() -> None:
    runtime, commands, _, _ = make_runtime()
    runtime.load(simple_plugin("a"))
    runtime.load(simple_plugin("b"))
    runtime.enable("a")
    runtime.enable("b")
    assert len(commands) == 2
    runtime.disable("a")
    assert len(commands) == 1
    assert "b_cmd" in commands


def test_events_fire_once_after_reload_cycles() -> None:
    """核心验收：load/enable/disable/enable/reload/reload 后事件只执行一次。"""
    runtime, _, _, bus = make_runtime()
    calls: list[str] = []
    plugin = Plugin(
        id="obs",
        subscriptions_spec=[("message.received", lambda e: calls.append("hit"), None)],
    )
    runtime.load(plugin)
    runtime.enable("obs")
    runtime.disable("obs")
    runtime.enable("obs")
    runtime.reload("obs")
    runtime.reload("obs")

    from dl_senpai.core.events import Event, EventTypes

    bus.publish(Event(type=EventTypes.MESSAGE_RECEIVED))
    assert calls == ["hit"], f"订阅被重复注册了：{len(calls)} 次"


def test_plugin_context_exposes_services() -> None:
    runtime, _, _, _ = make_runtime()
    runtime.services.register("thing", 42, owner="core")
    captured: list[object] = []
    plugin = Plugin(id="ctx", setup=lambda c: captured.append(c.get_service("thing")))
    runtime.load(plugin)
    assert captured == [42]


def test_list_sorted_by_priority() -> None:
    runtime, _, _, _ = make_runtime()
    runtime.load(Plugin(id="low", priority=200))
    runtime.load(Plugin(id="high", priority=10))
    assert [p.id for p in runtime.list()] == ["high", "low"]


def test_enabled_plugins_filter() -> None:
    runtime, _, _, _ = make_runtime()
    runtime.load(simple_plugin("a"))
    runtime.load(simple_plugin("b"))
    runtime.enable("a")
    assert [p.id for p in runtime.enabled_plugins()] == ["a"]


# ================================================================
# ServiceRegistry
# ================================================================


def test_service_register_and_get() -> None:
    reg = ServiceRegistry()
    reg.register("llm", "client", owner="core")
    assert reg.get("llm") == "client"
    assert reg.get("missing") is None
    assert reg.get("missing", "default") == "default"


def test_service_unregister() -> None:
    reg = ServiceRegistry()
    reg.register("a", 1, owner="p")
    assert reg.unregister("a") is True
    assert reg.unregister("a") is False
    assert reg.has("a") is False


def test_service_unregister_respects_owner() -> None:
    reg = ServiceRegistry()
    reg.register("a", 1, owner="p1")
    assert reg.unregister("a", owner="p2") is False
    assert reg.unregister("a", owner="p1") is True


def test_service_unregister_owner_bulk() -> None:
    reg = ServiceRegistry()
    reg.register("a", 1, owner="p")
    reg.register("b", 2, owner="p")
    reg.register("c", 3, owner="other")
    assert reg.unregister_owner("p") == 2
    assert reg.names() == ["c"]


def test_service_non_replaceable() -> None:
    reg = ServiceRegistry()
    reg.register("lock", 1, owner="core", replaceable=False)
    with pytest.raises(ValueError):
        reg.register("lock", 2, owner="other")


def test_service_require_raises() -> None:
    reg = ServiceRegistry()
    with pytest.raises(KeyError):
        reg.require("nope")


def test_service_replaceable_default() -> None:
    reg = ServiceRegistry()
    reg.register("a", 1)
    reg.register("a", 2)
    assert reg.get("a") == 2
