"""消息路由抽象：Command > Plugin > Agent 三级拦截。

**本阶段（Phase 2）不接管任何真实 QQ 消息。** 现有 priority 4/5/8/10/50/100
的 NoneBot matcher 继续独立工作，Router 只提供接口、实现与测试。
Phase 3 才会把真实消息接进来。

为什么要有这一层：现在「命令优先于插件优先于 AI」这个顺序是**隐式**的，
靠 NoneBot 的 priority 数字分散在 handlers.py 里（verify=4, checkin=5,
shop=5, private=8, mention=10, interrupt=50, sticker=100）。后果是：

- 想知道一条消息会走哪条路，必须通读 handlers.py；
- 新增一条命令要挑一个「不打架的数字」；
- 表情包收集器既在 priority 100 的 matcher 里跑，又在 `_handle_chat`
  内部被调用一次 → 同一条消息收集两次（Phase 1 审计 Bug 2）。

Router 把这个顺序显式化为**三级 stage**，每级有明确的语义：

    command  —— 确定性指令，命中即终止（签到/商店/好感度/认证）
    plugin   —— 插件式拦截，可声明 consume 决定是否终止
    agent    —— AI 兜底，需要满足触发条件（@/概率/冷却）

关键设计：`RouteResult.consumed` 与 `matched` 是**两个独立概念**。
- `matched=True, consumed=False`：我处理了，但后续级别可以继续跑
  （例如贴纸收集器：它处理了消息，但不该阻止 AI 回复）。
- `matched=True, consumed=True`：短路，后续级别不执行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

RouteStage = Literal["command", "plugin", "agent"]

# 级别执行顺序，数值越小越先执行。与原 matcher 的 priority 语义一致。
STAGE_ORDER: dict[str, int] = {
    "command": 100,
    "plugin": 200,
    "agent": 300,
}

# 命令的默认优先级（对应原 matcher priority 5 的签到/商店）
COMMAND_PRIORITY = 50
# 插件默认优先级
PLUGIN_PRIORITY = 100
# Agent 默认优先级（最低，兜底）
AGENT_PRIORITY = 900


@dataclass
class RouteContext:
    """一次路由判定所需的全部输入。

    Phase 2 刻意做成**与 NoneBot 解耦**的普通对象：字段都是从
    `MessageEvent` 提取出来的纯数据。这样 Router/Route 都能在无 driver 的
    单测环境直接构造，不需要 mock 一整个 Bot。
    """

    # 消息正文（已去掉 @ 段等富文本，保留纯文本）
    text: str = ""
    # 会话标识，沿用现有 memory 的 `group:123` / `private:9` 格式
    session_id: str = ""
    # 事件来源：group / private
    source: str = ""
    scope: Literal["group", "user", "private", "global"] = "global"
    group_id: str = ""
    user_id: str = ""
    self_id: str = ""
    message_id: str = ""
    # 是否 @ 了机器人
    is_tome: bool = False
    # 是否包含图片/表情等媒体
    has_images: bool = False
    sticker_count: int = 0
    # 原始事件对象（仅用于 Phase 3 迁移期透传给旧代码，新代码不应依赖）
    raw_event: Any = None
    bot: Any = None
    # 跨级别传递的附件（路由中间结果）
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class RouteResult:
    """一次路由判定结果。

    **`handled` 与 `consumed` 是两个独立概念**（Phase 3 第二十节），
    绝不能合并成一个 bool：

    | 场景 | handled | consumed | replied |
    |---|---|---|---|
    | 命令（签到） | true | true | true |
    | 旁路观察（表情采集） | true | **false** | false |
    | Agent 回复 | true | true | true |
    | 规则不匹配 | false | false | false |

    `handled`：本 handler 是否真的做了事。
    `consumed`：是否阻止后续 stage。
    典型反例就是表情采集器——它确实处理了消息（handled），
    但绝不能让签到命令或 AI 因此不执行（consumed=false）。

    Attributes:
        matched: 兼容字段，等价于 `handled`（Phase 2 起的旧名）。
        handled: 是否已处理。
        consumed: 是否终止后续级别。
        replied: 是否已自行发送回复（防止命令回复后 AI 再回一句）。
        reason: 命中/跳过的原因。
        stage: 命中的级别。
        handler: 命中的 handler 名（日志用）。
        priority: 声明的优先级。
        metadata: 附加信息。
    """

    matched: bool = False
    consumed: bool = False
    replied: bool = False
    handled: bool = False
    reason: str = ""
    stage: str = ""
    handler: str = ""
    priority: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 允许只用旧字段 `matched` 构造；自动同步到 `handled`
        if self.matched and not self.handled:
            self.handled = True
        elif self.handled and not self.matched:
            self.matched = True

    @classmethod
    def skip(cls, reason: str = "") -> "RouteResult":
        return cls(matched=False, handled=False, consumed=False, reason=reason)

    @classmethod
    def handle(
        cls,
        *,
        reason: str = "",
        consumed: bool = True,
        replied: bool = False,
    ) -> "RouteResult":
        return cls(
            matched=True,
            handled=True,
            consumed=consumed,
            replied=replied,
            reason=reason,
        )

    @classmethod
    def observe(cls, *, reason: str = "") -> "RouteResult":
        """旁路处理：已处理但不短路，也不回复。"""
        return cls(
            matched=True,
            handled=True,
            consumed=False,
            replied=False,
            reason=reason,
        )

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "handler": self.handler or None,
            "matched": self.matched,
            "handled": self.handled,
            "consumed": self.consumed,
            "replied": self.replied,
            "reason": self.reason,
        }


# handler 可以同步或异步返回 RouteResult
RouteHandler = Callable[[RouteContext], RouteResult | Awaitable[RouteResult]]


@dataclass
class Route:
    """一条注册的路由。

    Attributes:
        name: 唯一名（日志/事件用），例如 `checkin`、`shop`、`sticker_collect`。
        stage: command / plugin / agent。
        matcher: 判定函数，返回 True 表示这条路由愿意处理。
        handler: 实际处理函数。
        priority: 同 stage 内排序，数值小者先执行。
        consume: 命中后是否短路后续级别。
        owner: 归属插件名，Phase 4 插件 disable 时按 owner 批量卸载。
    """

    name: str
    stage: RouteStage
    matcher: Callable[[RouteContext], bool]
    handler: RouteHandler
    priority: int = PLUGIN_PRIORITY
    consume: bool = True
    owner: str = ""

    def matches(self, ctx: RouteContext) -> bool:
        try:
            return bool(self.matcher(ctx))
        except Exception:  # noqa: BLE001 — matcher 异常视为不匹配
            return False


class Observer:
    """旁路观察者：看到每条消息，但不参与短路。

    典型用途：表情采集、日志、埋点、统计。

    与 `Route` 的区别只有三点，但都很关键：
    1. 永不 consume（结构上保证，`dispatch_observers` 会强制归零）；
    2. 不受其他 stage 的 consume 影响；
    3. 默认 matcher 为"全部匹配"。
    """

    def __init__(
        self,
        name: str,
        handler: RouteHandler,
        *,
        matcher: Callable[[RouteContext], bool] | None = None,
        priority: int = AGENT_PRIORITY,
        owner: str = "",
    ) -> None:
        self.name = name
        self.handler = handler
        self.matcher = matcher or (lambda _ctx: True)
        self.priority = priority
        self.owner = owner

    def matches(self, ctx: RouteContext) -> bool:
        try:
            return bool(self.matcher(ctx))
        except Exception:  # noqa: BLE001 — matcher 异常视为不匹配
            return False


class Router:
    """按 stage + priority 顺序执行路由。

    执行语义：

        for stage in (command, plugin, agent):        # 固定三级
            for route in sorted(routes[stage]):       # 级别内按 priority
                if not route.matches(ctx): continue
                result = route.handler(ctx)
                if result.consumed: return result     # 短路
            # 该级别无人消费 → 进入下一级

    观察者（`dispatch_observers`）独立于本流程，永远全部执行。
    """

    def __init__(self, *, bus: Any = None) -> None:
        self._routes: dict[str, list[Route]] = {
            "command": [],
            "plugin": [],
            "agent": [],
        }
        self._observers: list[Observer] = []
        self._bus = bus

    # -- 注册 --

    def register(self, route: Route) -> Route:
        if route.stage not in self._routes:
            raise ValueError(f"未知 stage: {route.stage}")
        # 同名同 owner 视为重注册（reload 场景），先移除旧的
        self.unregister(route.name, owner=route.owner)
        self._routes[route.stage].append(route)
        self._routes[route.stage].sort(key=lambda r: (r.priority, r.name))
        return route

    def command(
        self,
        name: str,
        matcher: Callable[[RouteContext], bool],
        handler: RouteHandler,
        *,
        priority: int = COMMAND_PRIORITY,
        owner: str = "",
    ) -> Route:
        """注册命令路由（最高优先级，默认短路）。"""
        return self.register(
            Route(
                name=name,
                stage="command",
                matcher=matcher,
                handler=handler,
                priority=priority,
                consume=True,
                owner=owner,
            )
        )

    def plugin_route(
        self,
        name: str,
        matcher: Callable[[RouteContext], bool],
        handler: RouteHandler,
        *,
        priority: int = PLUGIN_PRIORITY,
        consume: bool = True,
        owner: str = "",
    ) -> Route:
        """注册插件路由（可用 consume 决定是否让 AI 继续处理）。"""
        return self.register(
            Route(
                name=name,
                stage="plugin",
                matcher=matcher,
                handler=handler,
                priority=priority,
                consume=consume,
                owner=owner,
            )
        )

    def agent_route(
        self,
        name: str,
        matcher: Callable[[RouteContext], bool],
        handler: RouteHandler,
        *,
        priority: int = AGENT_PRIORITY,
        owner: str = "",
    ) -> Route:
        """注册 Agent 兜底路由（最低优先级）。"""
        return self.register(
            Route(
                name=name,
                stage="agent",
                matcher=matcher,
                handler=handler,
                priority=priority,
                consume=True,
                owner=owner,
            )
        )

    def unregister(self, name: str, *, owner: str | None = None) -> int:
        """按名字移除路由；返回移除数量。

        owner 传入时会校验归属，防止插件 A 卸掉插件 B 的同名路由。
        """
        removed = 0
        for stage, routes in self._routes.items():
            keep = []
            for route in routes:
                same = route.name == name and (
                    owner is None or route.owner == owner
                )
                if same:
                    removed += 1
                else:
                    keep.append(route)
            self._routes[stage] = keep
        return removed

    def unregister_owner(self, owner: str) -> int:
        """按归属者批量卸载（插件 disable/reload）。"""
        if not owner:
            return 0
        removed = 0
        for stage, routes in self._routes.items():
            keep = []
            for route in routes:
                if route.owner == owner:
                    removed += 1
                else:
                    keep.append(route)
            self._routes[stage] = keep
        return removed

    def routes(self, stage: str | None = None) -> list[Route]:
        if stage is None:
            out: list[Route] = []
            for s in sorted(self._routes, key=lambda k: STAGE_ORDER[k]):
                out.extend(self._routes[s])
            return out
        return list(self._routes.get(stage, []))

    def clear(self) -> None:
        for stage in self._routes:
            self._routes[stage] = []
        self._observers = []

    # -- 观察者（旁路，不参与 consume 短路）--

    def observe(
        self,
        name: str,
        handler: RouteHandler,
        *,
        matcher: Callable[[RouteContext], bool] | None = None,
        priority: int = AGENT_PRIORITY,
        owner: str = "",
    ) -> "Observer":
        """注册一个**旁路观察者**。

        与 `plugin_route` 的根本区别：观察者**永远会看到消息**，
        无论命令阶段是否已经 consume。

        为什么必须有这个机制（Phase 3 第十七/十八节）：表情采集这类
        "埋点式"行为如果做成普通路由，那么"签到"命令 consume 之后
        采集器就再也看不到消息了——而用户在签到消息里发的表情同样
        应该被采集。观察者模式从结构上消除了这个问题，而不是靠
        "记得把 consume 设成 false"这种约定。
        """
        observer = Observer(
            name=name,
            handler=handler,
            matcher=matcher,
            priority=priority,
            owner=owner,
        )
        self.unregister_observer(name, owner=owner)
        self._observers.append(observer)
        self._observers.sort(key=lambda o: (o.priority, o.name))
        return observer

    def unregister_observer(self, name: str, *, owner: str | None = None) -> int:
        removed = 0
        keep: list[Observer] = []
        for obs in self._observers:
            if obs.name == name and (owner is None or obs.owner == owner):
                removed += 1
            else:
                keep.append(obs)
        self._observers = keep
        return removed

    def unregister_observer_owner(self, owner: str) -> int:
        if not owner:
            return 0
        keep = [o for o in self._observers if o.owner != owner]
        removed = len(self._observers) - len(keep)
        self._observers = keep
        return removed

    def observers(self) -> list["Observer"]:
        return list(self._observers)

    async def dispatch_observers(self, ctx: RouteContext) -> list[RouteResult]:
        """执行全部匹配的观察者；**不受任何 consume 影响**。

        异常隔离：单个观察者失败不影响其他观察者，也不影响主路由。
        """
        results: list[RouteResult] = []
        for obs in list(self._observers):
            if not obs.matches(ctx):
                continue
            result = await self._run_observer(obs, ctx)
            result.stage = "observer"
            result.handler = obs.name
            result.priority = obs.priority
            self._emit(result, ctx, obs)
            results.append(result)
        return results

    async def _run_observer(self, obs: "Observer", ctx: RouteContext) -> RouteResult:
        import inspect

        try:
            result = obs.handler(ctx)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001
            try:
                from ..observability.logging import log_event

                log_event(
                    "router.observer_failed",
                    level="warning",
                    observer=obs.name,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception:  # noqa: BLE001
                pass
            return RouteResult.skip(reason=f"observer_error:{type(exc).__name__}")
        if not isinstance(result, RouteResult):
            return RouteResult(
                matched=True, handled=True, consumed=False, replied=False,
                reason="implicit", stage="observer", handler=obs.name,
            )
        # 观察者永远不短路，强制归零 consumed
        result.consumed = False
        result.stage = "observer"
        return result

    # -- 执行 --

    async def dispatch(self, ctx: RouteContext) -> RouteResult:
        """按 Command > Plugin > Agent 顺序路由一条消息。

        观察者**不在此流程内**——由调用方在 `dispatch` 前后独立执行
        `dispatch_observers`，以保证旁路行为不被 consume 屏蔽。
        """
        for stage in sorted(self._routes, key=lambda k: STAGE_ORDER[k]):
            for route in self._routes[stage]:
                if not route.matches(ctx):
                    continue
                result = await self._run(route, ctx)
                result.stage = result.stage or stage
                result.handler = result.handler or route.name
                result.priority = route.priority
                self._emit(result, ctx, route)
                if result.consumed:
                    return result
                # 未消费：继续本级别内后续路由，以及更低的级别
            # 级别结束仍未消费 → 落入下一级
        return RouteResult.skip(reason="no_route_matched")

    async def _run(self, route: Route, ctx: RouteContext) -> RouteResult:
        import inspect

        try:
            result = route.handler(ctx)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 — 单条路由失败不应中断调度
            try:
                from ..observability.logging import log_event

                log_event(
                    "router.handler_failed",
                    level="warning",
                    route=route.name,
                    stage=route.stage,
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception:  # noqa: BLE001
                pass
            return RouteResult.skip(reason=f"handler_error:{type(exc).__name__}")
        if not isinstance(result, RouteResult):
            # 容错：handler 返回 None/True 等非标准值时按「已处理且短路」处理
            return RouteResult(
                matched=True,
                consumed=route.consume,
                reason="implicit",
                stage=route.stage,
                priority=route.priority,
            )
        return result

    def _emit(self, result: RouteResult, ctx: RouteContext, route: Route) -> None:
        """把路由结果作为事件发布（供日志/指标订阅）。"""
        if self._bus is None:
            return
        try:
            from .events import Event, EventTypes

            event_type = (
                EventTypes.ROUTE_MATCHED if result.matched else EventTypes.ROUTE_SKIPPED
            )
            self._bus.publish(
                Event(
                    type=event_type,
                    source="router",
                    actor=ctx.user_id,
                    scope=ctx.scope,
                    payload={
                        "route": route.name,
                        "stage": route.stage,
                        "reason": result.reason,
                        "consumed": result.consumed,
                        "session_id": ctx.session_id,
                    },
                )
            )
        except Exception:  # noqa: BLE001 — 事件发布失败不影响路由
            pass


__all__ = [
    "RouteContext",
    "RouteResult",
    "RouteHandler",
    "Route",
    "Router",
    "RouteStage",
    "STAGE_ORDER",
    "COMMAND_PRIORITY",
    "PLUGIN_PRIORITY",
    "AGENT_PRIORITY",
]
