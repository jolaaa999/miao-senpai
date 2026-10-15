"""插件定义与运行时上下文。

Phase 3 任务 C/D。目标不是"做一套插件框架"，而是让**新增功能的接入点
从 handlers.py 移出**：新功能 = 一个新的 Plugin 定义 + 注册命令/订阅。

关键设计：Observer 与 Routing Handler 分离（Phase 3 第十八节）。

    旁路观察（表情采集 / 日志 / 埋点）
        → subscriptions：订阅 EventBus 事件，**永远能看到消息**，
          不受任何 consume 影响。

    路由处理（命令 / 拦截 / AI）
        → commands + routes：参与 Router 调度，可 consume。

为什么必须分开：如果表情采集器做成 Plugin Stage 的路由处理器，
那么"签到"命令 consume 之后，采集器**根本看不到这条消息**——
而用户在签到消息里发的表情同样应该被采集。观察者模式天然解决这个问题。

另外，通知类事件（入群/退群）没有"路由"语义，只适合 Observer。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ...observability.logging import log_event
from ...observability.metrics import metrics
from ..commands.models import Command
from ..event_bus import Event, EventHandler, EventFilter
from ..router import Route

# 插件上下文传入的运行时句柄
PluginSetup = Callable[["PluginContext"], Any]


@dataclass
class PluginContext:
    """插件运行时上下文。

    插件**只依赖这个对象**，不直接 import 其他插件、不直接拿全局单例。
    这样 `disable()` 时清理干净，`reload()` 时也不会残留状态。

    Attributes:
        plugin_id: 当前插件 id。
        services: ServiceRegistry（取 config / llm / storage 等能力）。
        bus: EventBus。
        router: Router。
        commands: CommandRegistry。
        logger: 结构化日志入口。
    """

    plugin_id: str
    services: Any = None
    bus: Any = None
    router: Any = None
    commands: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    # -- 能力获取 --

    def get_service(self, name: str, default: Any = None) -> Any:
        if self.services is None:
            return default
        return self.services.get(name, default)

    def get_config(self) -> Any:
        """取当前配置（走 Phase 2 缓存，不会重复读 .env）。"""
        from ...config_runtime.runtime import get_config

        return get_config()

    def get_storage(self, path: Any) -> Any:
        """取一个 JsonStore（原子写 + 锁）。"""
        from ...storage import JsonStore

        return JsonStore(path)

    # -- 通信 --

    def emit(self, event_type: str, **payload: Any) -> int:
        """发布事件（跨模块通信，不需要返回值）。"""
        if self.bus is None:
            return 0
        return self.bus.publish(
            Event(type=event_type, source=f"plugin:{self.plugin_id}", payload=payload)
        )

    def subscribe(
        self,
        pattern: str,
        handler: EventHandler,
        *,
        where: EventFilter | None = None,
    ) -> Any:
        """订阅事件（旁路观察）。owner 自动绑定为当前插件。"""
        if self.bus is None:
            return None
        return self.bus.subscribe(pattern, handler, where=where, owner=self.plugin_id)

    # -- 日志 --

    def log(self, event: str, level: str = "info", **fields: Any) -> None:
        log_event(event, level=level, plugin=self.plugin_id, **fields)


@dataclass
class Plugin:
    """一个插件。

    Attributes:
        id: 唯一 id（也是 owner 名，用于批量清理）。
        name: 人类可读名。
        version: 版本号。
        namespace: 命名空间（命令/路由前缀）。
        priority: 插件阶段内排序，数值小者先。
        exclusive: 命中且 consume 时是否独占（等价于 route.consume）。
        commands: 该插件注册的命令（注册时 owner 会设为 id）。
        routes: 该插件注册的路由。
        subscriptions: 旁路事件订阅。
        subscriptions_spec: 声明式订阅规格，enable 时由 runtime 建立。
        setup: 首次 load 时调用一次的初始化钩子。
        teardown: unload 时调用。
    """

    id: str
    name: str = ""
    version: str = "0.1.0"
    namespace: str = ""
    priority: int = 100
    # 期望状态：load() 后是否自动激活。
    # 注意与 `active` 的区别——`enabled` 是"声明意图"，`active` 是"当前事实"。
    # 只 load 不 enable 的插件，enabled=True 但 active=False。
    enabled: bool = True
    exclusive: bool = False

    commands: list[Command] = field(default_factory=list)
    routes: list[Route] = field(default_factory=list)
    subscriptions_spec: list[tuple[str, EventHandler, EventFilter | None]] = field(
        default_factory=list
    )

    setup: PluginSetup | None = None
    teardown: Callable[[], Any] | None = None

    # 运行时填充：已建立的订阅句柄（disable 时逐个取消）
    _subscriptions: list[Any] = field(default_factory=list, repr=False)
    _loaded: bool = field(default=False, repr=False)
    # 是否已真正激活（注册项已在各注册表中）
    _active: bool = field(default=False, repr=False)
    _registered_commands: list[str] = field(default_factory=list, repr=False)
    _registered_routes: list[str] = field(default_factory=list, repr=False)

    @property
    def active(self) -> bool:
        """当前是否已激活（区别于声明意图 `enabled`）。"""
        return self._active

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self.id
        if not self.namespace:
            self.namespace = self.id

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "plugin": self.id,
            "version": self.version,
            "namespace": self.namespace,
            "priority": self.priority,
        }


__all__ = ["Plugin", "PluginContext", "PluginSetup"]
