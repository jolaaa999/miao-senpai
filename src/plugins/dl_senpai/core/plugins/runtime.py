"""Plugin Runtime：插件生命周期与 owner 追踪。

Phase 3 任务 C/D 的核心。必须满足的验收条件（第十三节）：

    load → enable → disable → enable → reload → reload
    之后同一事件只能执行一次

实现方式：**所有注册动作都带 owner，所有清理动作都按 owner 批量执行**。

    enable()  → commands.register(cmd) / bus.subscribe(...) / router.register(route)
    disable() → commands.unregister_owner(id)
                bus.unsubscribe_owner(id)
                router.unregister_owner(id)
                services.unregister_owner(id)   ← 可选
                + 取消订阅句柄

只要 enable 时登记的每一项都能被 owner 找回，reload 就不会残留。
`CommandRegistry.register` 与 `Router.register` 都是"同名替换"语义，
`EventBus.subscribe` 则会因 owner 清理而不会累积——三者共同保证幂等。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...observability.logging import log_event
from ...observability.metrics import metrics
from .models import Plugin, PluginContext


class PluginError(Exception):
    """插件生命周期错误。"""


@dataclass
class PluginRuntime:
    """插件注册与生命周期管理。

    Args:
        commands: CommandRegistry
        router: Router
        bus: EventBus
        services: ServiceRegistry
    """

    commands: Any = None
    router: Any = None
    bus: Any = None
    services: Any = None

    _plugins: dict[str, Plugin] = field(default_factory=dict)
    _contexts: dict[str, PluginContext] = field(default_factory=dict)

    # -- load --

    def load(self, plugin: Plugin) -> Plugin:
        """装载插件（只登记定义，不激活）。

        重复 load 同一 id 会**替换**定义并重置状态，不会累积。
        """
        if plugin.id in self._plugins:
            # 已被装载过：先彻底卸载旧定义，保证干净
            self.unload(plugin.id)
        self._plugins[plugin.id] = plugin
        plugin._loaded = True
        log_event("plugin.loaded", **plugin.as_log_fields())
        metrics.incr("plugin.loaded")
        if plugin.setup is not None:
            ctx = self._build_context(plugin)
            try:
                plugin.setup(ctx)
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "plugin.setup_failed",
                    level="warning",
                    plugin=plugin.id,
                    error=f"{type(exc).__name__}: {exc}",
                )
        return plugin

    # -- enable --

    def enable(self, plugin_id: str) -> bool:
        """激活插件：注册命令 / 路由 / 订阅。"""
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            return False
        if plugin.active and self._is_registered(plugin):
            # 已激活且注册项完好 → 幂等返回，避免重复注册
            return True
        self._register_all(plugin)
        plugin._active = True
        log_event("plugin.enabled", **plugin.as_log_fields())
        metrics.incr("plugin.enabled")
        return True

    def _register_all(self, plugin: Plugin) -> None:
        """把插件的全部能力登记到对应注册表。"""
        # 1) 命令
        if self.commands is not None:
            for cmd in plugin.commands:
                cmd.owner = plugin.id
                if not cmd.namespace or cmd.namespace == "default":
                    cmd.namespace = plugin.namespace
                self.commands.register(cmd)
                plugin._registered_commands.append(cmd.name)

        # 2) 路由
        if self.router is not None:
            for route in plugin.routes:
                route.owner = plugin.id
                self.router.register(route)
                plugin._registered_routes.append(route.name)

        # 3) 旁路订阅
        if self.bus is not None:
            for pattern, handler, where in plugin.subscriptions_spec:
                sub = self.bus.subscribe(
                    pattern, handler, where=where, owner=plugin.id
                )
                if sub is not None:
                    plugin._subscriptions.append(sub)

    def _is_registered(self, plugin: Plugin) -> bool:
        """检查注册项是否仍然存在（用于判断是否需要重新注册）。"""
        if self.commands is None or not plugin.commands:
            return True
        return all(c.name in self.commands for c in plugin.commands)

    # -- disable --

    def disable(self, plugin_id: str) -> bool:
        """停用插件并**彻底清理**它注册的一切。"""
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            return False
        self._cleanup(plugin)
        plugin._active = False
        log_event("plugin.disabled", **plugin.as_log_fields())
        metrics.incr("plugin.disabled")
        return True

    def _cleanup(self, plugin: Plugin) -> None:
        """按 owner 清理所有注册项。这是"不重复注册"的唯一保证。"""
        # 命令
        if self.commands is not None:
            self.commands.unregister_owner(plugin.id)
        plugin._registered_commands.clear()

        # 路由
        if self.router is not None:
            self.router.unregister_owner(plugin.id)
        plugin._registered_routes.clear()

        # 订阅：先按 owner 批量，再逐个句柄兜底
        if self.bus is not None:
            self.bus.unsubscribe_owner(plugin.id)
        for sub in plugin._subscriptions:
            try:
                self.bus.unsubscribe(sub) if self.bus is not None else None
            except Exception:  # noqa: BLE001
                pass
        plugin._subscriptions.clear()

    # -- unload --

    def unload(self, plugin_id: str) -> bool:
        """停用并移除插件定义。"""
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            return False
        self._cleanup(plugin)
        if plugin.teardown is not None:
            try:
                plugin.teardown()
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "plugin.teardown_failed",
                    level="warning",
                    plugin=plugin_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
        self._contexts.pop(plugin_id, None)
        del self._plugins[plugin_id]
        log_event("plugin.unloaded", plugin=plugin_id)
        return True

    # -- reload --

    def reload(self, plugin_id: str) -> bool:
        """重新加载：先彻底清理，再按原 enabled 状态恢复。

        关键：清理走 `_cleanup`（owner 批量），因此无论 reload 多少次，
        注册表里该插件永远只有一份。
        """
        plugin = self._plugins.get(plugin_id)
        if plugin is None:
            return False
        was_enabled = plugin.active
        self._cleanup(plugin)
        if was_enabled:
            self._register_all(plugin)
        log_event("plugin.reloaded", **plugin.as_log_fields(), enabled=was_enabled)
        metrics.incr("plugin.reloaded")
        return True

    def reload_all(self) -> int:
        """重载全部插件；返回数量。"""
        count = 0
        for pid in list(self._plugins):
            if self.reload(pid):
                count += 1
        return count

    # -- 查询 --

    def get(self, plugin_id: str) -> Plugin | None:
        return self._plugins.get(plugin_id)

    def list(self) -> list[Plugin]:
        return sorted(self._plugins.values(), key=lambda p: (p.priority, p.id))

    def enabled_plugins(self) -> list[Plugin]:
        return [p for p in self.list() if p.active]

    def ids(self) -> list[str]:
        return sorted(self._plugins)

    def __contains__(self, plugin_id: object) -> bool:
        return plugin_id in self._plugins

    def __len__(self) -> int:
        return len(self._plugins)

    # -- 上下文 --

    def context_for(self, plugin_id: str) -> PluginContext | None:
        return self._contexts.get(plugin_id)

    def _build_context(self, plugin: Plugin) -> PluginContext:
        ctx = PluginContext(
            plugin_id=plugin.id,
            services=self.services,
            bus=self.bus,
            router=self.router,
            commands=self.commands,
        )
        self._contexts[plugin.id] = ctx
        return ctx


__all__ = ["PluginRuntime", "PluginError"]
