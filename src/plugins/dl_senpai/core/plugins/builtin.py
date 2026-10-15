"""内置插件定义：把现有能力登记为真正的 Plugin。

Phase 3 任务 C 的"真实已迁移插件"——不是空壳，而是把已经跑通的行为
（内置命令、表情采集、入群欢迎、退群清理）用 Plugin 抽象重新表达，
验证 Plugin Runtime 的 load/enable/disable/reload 在真实场景下可用。

关键点：`Plugin.commands` 必须**真的包含**那些 Command 对象，
否则 `PluginRuntime.disable()` 按 owner 清理时找不到它们。
"""

from __future__ import annotations

from ...observability.logging import log_event
from .models import Plugin

PLUGIN_COMMANDS = "builtin_commands"
PLUGIN_OBSERVERS = "builtin_observers"


def register_builtin_plugins(runtime: object) -> list[Plugin]:
    """把内置能力登记为 Plugin，建立真实的 owner 归属。

    做法：直接从**已注册**的命令表里取出属于本插件的命令，
    构造 Plugin 时带上这些对象。这样 `disable()` 能真正清理掉它们。

    （不能凭空 `build_builtin_commands()` 再塞进 Plugin——那会产生
    两批 id 相同但对象不同的 Command，清理时对不上。）
    """
    from .runtime import PluginRuntime

    plugins_runtime: PluginRuntime | None = getattr(runtime, "plugins", None)
    if plugins_runtime is None:
        return []

    commands_registry = getattr(runtime, "commands", None)
    router = getattr(runtime, "router", None)

    owned_commands = []
    if commands_registry is not None:
        owned_commands = [
            c for c in commands_registry.list() if c.owner == PLUGIN_COMMANDS
        ]

    owned_observers = []
    if router is not None:
        owned_observers = [
            o for o in router.observers() if o.owner == PLUGIN_OBSERVERS
        ]

    commands_plugin = Plugin(
        id=PLUGIN_COMMANDS,
        name="内置命令",
        namespace="builtin",
        priority=50,
        exclusive=True,
        commands=owned_commands,
    )

    observers_plugin = Plugin(
        id=PLUGIN_OBSERVERS,
        name="内置观察者",
        namespace="builtin",
        priority=900,
        exclusive=False,
    )

    plugins: list[Plugin] = [commands_plugin, observers_plugin]
    for plugin in plugins:
        plugins_runtime.load(plugin)
        # 这些能力在建 Runtime 时已经注册完毕，因此直接标记为已激活，
        # 不再走一次 enable()（那会重复注册）。
        plugin._active = True

    log_event(
        "plugin.builtin_ready",
        plugins=[p.id for p in plugins],
        commands=[c.name for c in owned_commands],
        observers=[o.name for o in owned_observers],
    )
    return plugins


__all__ = [
    "register_builtin_plugins",
    "PLUGIN_OBSERVERS",
    "PLUGIN_COMMANDS",
]
