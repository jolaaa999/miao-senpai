"""Extension 声明式登记：一次装载 / 一次摘除。

这层要守住的是**归属一致性**：
装载时谁写的 owner、摘除时按什么 owner 清，必须是同一个字符串。
写错一个字符，就会表现为「停用插件后工具还在、命令还能用」，
而这种问题只在真的去停用时才暴露。
"""

from __future__ import annotations

import re

from dl_senpai.agent.tools.registry import ToolDefinition, ToolRegistry
from dl_senpai.core.commands.models import Command
from dl_senpai.core.commands.registry import CommandRegistry
from dl_senpai.core.extensions import Extension, ExtensionRegistry
from dl_senpai.core.router import Observer, Route, Router


class _FakeRuntime:
    """只装四张注册表的运行时（不触发完整 Runtime.build）。"""

    def __init__(self) -> None:
        self.commands = CommandRegistry()
        self.tools = ToolRegistry()
        self.router = Router()


def _command(name: str = "demo") -> Command:
    return Command(name=name, patterns=[re.compile(r"^demo$")], handler=lambda ctx, m: None)


def _tool(name: str = "demo_tool") -> ToolDefinition:
    return ToolDefinition(name=name, description="demo")


def _route(name: str = "demo_route") -> Route:
    return Route(
        name=name,
        stage="plugin",
        matcher=lambda ctx: False,
        handler=lambda ctx: None,
    )


def test_apply_registers_all_four_kinds_with_same_owner() -> None:
    runtime = _FakeRuntime()
    registry = ExtensionRegistry(runtime)
    ext = Extension(
        id="demo",
        commands=[_command()],
        tools=[_tool()],
        routes=[_route()],
        observers=[Observer("demo_obs", lambda ctx: None)],
    )
    registry.apply(ext)

    assert runtime.commands.get("demo").owner == "demo"
    assert runtime.tools.get("demo_tool").owner == "demo"
    assert [r.owner for r in runtime.router.routes() if r.name == "demo_route"] == ["demo"]
    assert [o.owner for o in runtime.router.observers() if o.name == "demo_obs"] == ["demo"]


def test_remove_clears_everything() -> None:
    """摘除必须一次干净：命令、工具、路由、观察者都不留。"""
    runtime = _FakeRuntime()
    registry = ExtensionRegistry(runtime)
    registry.apply(
        Extension(
            id="demo",
            commands=[_command()],
            tools=[_tool()],
            routes=[_route()],
            observers=[Observer("demo_obs", lambda ctx: None)],
        )
    )
    registry.remove("demo")

    assert runtime.commands.get("demo") is None
    assert runtime.tools.get("demo_tool") is None
    assert not [r for r in runtime.router.routes() if r.name == "demo_route"]
    assert not [o for o in runtime.router.observers() if o.name == "demo_obs"]
    assert len(registry) == 0


def test_apply_is_idempotent() -> None:
    """重复装载同一扩展（reload 场景）不该产生两份注册项。"""
    runtime = _FakeRuntime()
    registry = ExtensionRegistry(runtime)
    registry.apply(Extension(id="demo", tools=[_tool()]))
    registry.apply(Extension(id="demo", tools=[_tool()]))
    assert len([t for t in runtime.tools.list() if t.name == "demo_tool"]) == 1
    assert len(registry) == 1


def test_explicit_owner_is_not_overwritten() -> None:
    """扩展自己声明了 owner 时不许覆盖——那会让既有的批量清理失效。"""
    runtime = _FakeRuntime()
    registry = ExtensionRegistry(runtime)
    tool = _tool()
    tool.owner = "high_risk_layer"
    registry.apply(Extension(id="demo", tools=[tool]))
    assert runtime.tools.get("demo_tool").owner == "high_risk_layer"


def test_empty_id_is_rejected() -> None:
    """id 就是 owner：空 id 等于"永远摘不掉"，必须当场拒绝。"""
    import pytest

    with pytest.raises(ValueError):
        ExtensionRegistry(_FakeRuntime()).apply(Extension(id=""))


def test_describe_is_serializable() -> None:
    runtime = _FakeRuntime()
    registry = ExtensionRegistry(runtime)
    registry.apply(Extension(id="mcp:web", name="MCP web", source="mcp", tools=[_tool()]))
    described = registry.describe()
    assert described == [
        {"id": "mcp:web", "name": "MCP web", "source": "mcp", "commands": 0, "tools": 1, "routes": 0, "observers": 0}
    ]
