"""Runtime：把 Phase 2/3 的基础设施组装成一个可用整体，并注册内置命令。

职责边界：
- **组装**：创建 Router / EventBus / CommandRegistry / PluginRuntime /
  ServiceRegistry / SessionExecutor，互相注入。
- **注册内置命令**：调用 `register_builtin_commands`。
- **注册观察者**：表情采集等旁路行为。
- **不包含**：任何业务逻辑。业务仍在 handlers.py 与各业务模块里。

为什么需要这一层：Phase 3 之前，全局单例散落在各处（`get_registry()`、
`get_services()`、`get_executor()`），没有统一入口初始化。Runtime 把它们
收口，让测试可以构造一个隔离实例，也让启动顺序显式化。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..observability.logging import log_event
from .agent_trigger import AgentTriggerPolicy
from .commands.builtin import register_builtin_commands
from .commands.registry import CommandRegistry
from .event_bus import EventBus
from .plugins.runtime import PluginRuntime
from .router import Router
from .services import (
    SERVICE_AGENT,
    SERVICE_COMMANDS,
    SERVICE_CONFIG,
    SERVICE_EVENT_BUS,
    SERVICE_ROUTER,
    SERVICE_SESSIONS,
    SERVICE_TOOLS,
    ServiceRegistry,
)
from .session.executor import SessionExecutor


@dataclass
class Runtime:
    """系统运行时的容器。

    所有子系统在此组装，通过 `services` 暴露给插件与业务代码。
    """

    commands: CommandRegistry = field(default_factory=CommandRegistry)
    bus: EventBus = field(default_factory=EventBus)
    router: Router = field(default_factory=Router)
    services: ServiceRegistry = field(default_factory=ServiceRegistry)
    sessions: SessionExecutor = field(default_factory=SessionExecutor)
    plugins: Any = None
    agent_trigger: AgentTriggerPolicy = field(default_factory=AgentTriggerPolicy)
    # Phase 4：Tool Registry 与 Agent Runtime（构建失败时为 None）
    tools: Any = None
    agent: Any = None
    # Phase 5：Agent 执行入口（模式 + fallback 边界），懒构建
    agent_entry: Any = None

    _built: bool = False

    def build(self) -> "Runtime":
        """组装并注册内置能力。幂等：重复调用不会重复注册。"""
        if self._built:
            return self

        # Router 需要 bus 来发 route.matched / route.skipped 事件
        self.router._bus = self.bus

        # PluginRuntime 拿到全部注册表，才能在 disable 时按 owner 清理
        self.plugins = PluginRuntime(
            commands=self.commands,
            router=self.router,
            bus=self.bus,
            services=self.services,
        )

        # 内置命令：正则逐字来自 checkin/shop/affection。
        # owner 设为 builtin_commands，让 PluginRuntime 能整体停用。
        register_builtin_commands(self.commands, owner="builtin_commands")

        # 内置观察者：旁路行为，永不短路（Phase 3 第十八节）
        self._register_builtin_observers()

        # 内置路由：命令 stage（走 Registry）+ Agent stage（legacy bridge）
        self._register_builtin_routes()

        # 内置插件：登记 owner 归属，让 PluginRuntime.disable 能整体停用
        self._register_builtin_plugins()

        # Phase 4：Tool Registry + Agent Runtime
        self._build_agent_layer()

        # 服务注册：让插件通过 ServiceRegistry 拿能力，而不是互相 import
        self.services.register(SERVICE_CONFIG, _config_provider, owner="core")
        self.services.register(SERVICE_COMMANDS, self.commands, owner="core")
        self.services.register(SERVICE_ROUTER, self.router, owner="core")
        self.services.register(SERVICE_EVENT_BUS, self.bus, owner="core")
        self.services.register(SERVICE_SESSIONS, self.sessions, owner="core")

        self._built = True
        log_event(
            "runtime.ready",
            commands=len(self.commands),
            namespaces=self.commands.namespaces(),
        )
        return self

    def _build_agent_layer(self) -> None:
        """构建 Phase 4 的 Agent 层：ToolRegistry + AgentRuntime。

        容错：Agent 层构建失败不应阻止 bot 启动——此时 AgentEntry 会把
        请求交给 legacy fallback（见 agent/entry.py）。
        """
        try:
            from ..agent.runtime import AgentRuntime
            from ..agent.tools.builtin import register_production_tools
            from ..agent.tools.creator_tools import register_creator_tools
            from ..agent.tools.high_risk import register_high_risk_tools
            from ..agent.tools.memory_tool import register_memory_tool
            from ..agent.tools.registry import get_tool_registry
            from ..knowledge_setup import register_knowledge_tool

            self.tools = get_tool_registry()
            if len(self.tools) == 0:
                register_production_tools(self.tools)
            # 知识工具单独注册。它默认不可见 —— 只有上下文里带了
            # knowledge_service 才会被 visible_tools 放行（见 knowledge_setup）。
            # 这里放在 len==0 之外，是因为注册表可能已被别的引导路径填过。
            register_knowledge_tool(self.tools)
            # 长期记忆检索同样单独注册：可见性由 memory 开关注导，
            # 与那 8 个内置工具的数量/executor 契约解耦。
            register_memory_tool(self.tools)
            # 高风险工具（文件/命令/代码）同样独立注册。注册不等于可用：
            # permissions.HIGH_RISK_TOOLS 会把它们对非管理员整个隐藏。
            register_high_risk_tools(self.tools)
            # 生成层（Word/PPT）：对**所有群友**可见。它只接受结构化文本，
            # 参数里没有路径也没有命令，与管理员专属的高危层是两套东西。
            register_creator_tools(self.tools)
            self.agent = AgentRuntime(registry=self.tools)
            self.services.register(SERVICE_TOOLS, self.tools, owner="core")
            self.services.register(SERVICE_AGENT, self.agent, owner="core")
            log_event(
                "agent.layer_ready",
                tools=len(self.tools),
                markers=sorted(self.tools.markers()),
            )
        except Exception as exc:  # noqa: BLE001
            self.tools = None
            self.agent = None
            log_event(
                "agent.layer_failed",
                level="warning",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _register_builtin_plugins(self) -> None:
        """把内置能力登记为 Plugin，建立 owner 归属。

        这样 `plugins.disable("builtin_commands")` 就能一次性停掉全部内置
        命令——Plugin Runtime 的 disable 语义在真实功能上得到验证，
        而不是只有测试里的假插件。
        """
        from .plugins.builtin import register_builtin_plugins

        try:
            register_builtin_plugins(self)
        except Exception as exc:  # noqa: BLE001 — 插件登记失败不应阻断启动
            log_event(
                "plugin.builtin_register_failed",
                level="warning",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _register_builtin_observers(self) -> None:
        """注册内置旁路观察者。

        这些行为在重构前是 `block=False` 的 NoneBot matcher。它们的特点是
        **只看不做决策**——采集、统计、通知，不参与"这条消息该谁回"。
        因此迁到 Observer 而不是 Route：结构上保证不会被 consume 屏蔽。
        """
        from ..handlers import (
            observe_notice_leave,
            observe_notice_welcome,
            observe_recent_chat,
            observe_stickers,
        )

        # 表情采集：所有消息（群+私聊），永不短路
        self.router.observe(
            "sticker_collector",
            observe_stickers,
            matcher=_is_message_ctx,
            priority=900,
            owner="builtin_observers",
        )
        # 当天聊天流水：所有消息（群+私聊），永不短路。
        # 采集和表情分开两个观察者而不是塞进一个：职责不同、开关也不同，
        # 关掉表情采集不该连带把背景流水一起关掉。
        self.router.observe(
            "recent_chat",
            observe_recent_chat,
            matcher=_is_message_ctx,
            priority=890,
            owner="builtin_observers",
        )
        # 入群欢迎 / 认证注册：仅群成员增加通知
        self.router.observe(
            "welcome_notice",
            observe_notice_welcome,
            matcher=_is_group_increase,
            priority=100,
            owner="builtin_observers",
        )
        # 退群清理：仅群成员减少通知
        self.router.observe(
            "leave_notice",
            observe_notice_leave,
            matcher=_is_group_decrease,
            priority=100,
            owner="builtin_observers",
        )

    def _register_builtin_routes(self) -> None:
        """注册内置路由：Command stage（Registry 驱动）+ Agent stage（legacy bridge）。

        命令 stage 只有一个路由——它把整张 CommandRegistry 当作匹配源。
        这样"新增命令"只需注册命令，不必再动路由表。

        Agent stage 同理只有一个路由：内部先跑 `AgentTriggerPolicy`，
        判定为 NONE 就直接跳过（等价于旧的"概率没中"）。
        """
        from .router import Route, RouteResult

        async def command_route(ctx: Any) -> RouteResult:
            """命令 stage：查 CommandRegistry。"""
            invocation = self.commands.match(
                getattr(ctx, "plain_text", "") or "", ctx
            )
            if invocation is None:
                return RouteResult.skip(reason="no_command")
            command = invocation.command
            log_event(
                "command.invoked",
                session_id=getattr(ctx, "session_id", "") or None,
                **invocation.as_log_fields(),
            )
            try:
                await _maybe_await(command.handler(ctx, invocation.match))
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "command.failed",
                    level="warning",
                    command=command.name,
                    error=f"{type(exc).__name__}: {exc}",
                )
                # 命令失败不短路：让 AI 有机会兜底，避免用户完全没反应
                return RouteResult.skip(reason=f"command_error:{type(exc).__name__}")
            return RouteResult.handle(
                reason=f"command:{command.name}",
                consumed=command.consume,
                replied=True,
            )

        async def agent_route(ctx: Any) -> RouteResult:
            """Agent stage：触发判定 + legacy bridge。"""
            if getattr(ctx, "message_type", "") not in {"group", "private"}:
                return RouteResult.skip(reason="not_message")

            decision = self.agent_trigger.decide(ctx)
            self.agent_trigger.log_decision(decision, ctx)
            if not decision.should_run:
                return RouteResult.skip(reason=decision.reason or "no_trigger")

            # 走模块属性而非提前 import 的函数名：
            # 这样测试与运维可以 patch `legacy_bridge.handle_agent_message`
            # 替换 Agent 行为，而不必改动 Router 本身。
            from .. import legacy_bridge

            ran = await legacy_bridge.handle_agent_message(ctx, trigger=decision)
            if not ran:
                # 背压拒绝：不算处理成功，但仍短路（避免重复入队）
                return RouteResult.handle(
                    reason=f"agent_dropped:{decision.kind.value}", consumed=True
                )
            return RouteResult.handle(
                reason=f"agent:{decision.kind.value}",
                consumed=True,
                replied=True,
            )

        self.router.register(
            Route(
                name="command_registry",
                stage="command",
                matcher=lambda _ctx: True,
                handler=command_route,
                priority=100,
                consume=True,
                owner="builtin",
            )
        )
        self.router.register(
            Route(
                name="agent_legacy_bridge",
                stage="agent",
                matcher=lambda _ctx: True,
                handler=agent_route,
                priority=900,
                consume=True,
                owner="builtin",
            )
        )

    async def dispatch(self, ctx: Any) -> Any:
        """标准消息处理入口：观察者 → Router。

        顺序是刻意的（Phase 3 第十九节）：

            message.received
                   |
           +-------+-------+
           |               |
        Observers        Router
        (永不短路)     Command → Plugin → Agent

        观察者**先于**路由执行，因此即便命令 consume 了消息，
        表情采集等旁路行为也已经完成。这是"旁路观察者不会被
        短路屏蔽"的结构性保证。
        """
        # 1) 旁路观察者：永远执行
        await self.router.dispatch_observers(ctx)
        # 2) 主路由：Command → Plugin → Agent
        return await self.router.dispatch(ctx)


async def _maybe_await(value: Any) -> Any:
    """等待可能为协程的返回值。"""
    import inspect

    if inspect.isawaitable(value):
        return await value
    return value


def _is_message_ctx(ctx: Any) -> bool:
    """观察者 matcher：仅普通消息（群/私聊）。"""
    return getattr(ctx, "message_type", "") in {"group", "private"}


def _is_group_increase(ctx: Any) -> bool:
    return getattr(ctx, "notice_type", "") == "notice.group_increase"


def _is_group_decrease(ctx: Any) -> bool:
    return getattr(ctx, "notice_type", "") == "notice.group_decrease"


def _config_provider() -> Any:
    """配置服务提供者。

    注册成函数而不是配置对象，是因为配置可能 reload（`config_version` 变化），
    延迟取值能永远拿到最新配置，而缓存的 config 对象会在 reload 后过期。
    """
    from ..config_runtime.runtime import get_config

    return get_config()


# -- 全局单例 --

_runtime: Runtime | None = None


def get_runtime() -> Runtime:
    global _runtime
    if _runtime is None:
        _runtime = Runtime().build()
    return _runtime


def reset_runtime(runtime: Runtime | None = None) -> None:
    """替换全局运行时（测试用）。"""
    global _runtime
    _runtime = runtime


__all__ = ["Runtime", "get_runtime", "reset_runtime"]
