"""Phase 3 第二层/第四层：Session Executor 并发测试 + Router/Observer 集成测试。"""

from __future__ import annotations

import asyncio

import pytest

from dl_senpai.core.router import Observer, Route, RouteContext, RouteResult, Router
from dl_senpai.core.session.executor import QueueFullError, SessionExecutor


# ================================================================
# Session Executor：同 session 串行、不同 session 并发
# ================================================================


@pytest.mark.asyncio
async def test_same_session_runs_serially() -> None:
    """同一 session 的两条任务必须有序，不能交错。"""
    ex = SessionExecutor()
    order: list[str] = []

    async def task(name: str, delay: float) -> None:
        order.append(f"{name}:start")
        await asyncio.sleep(delay)
        order.append(f"{name}:end")

    await ex.submit("s1", lambda: task("a", 0.05))
    await ex.submit("s1", lambda: task("b", 0.01))
    await ex.drain()

    # 必须是 a 完整跑完，b 才开始
    assert order == ["a:start", "a:end", "b:start", "b:end"]


@pytest.mark.asyncio
async def test_different_sessions_run_concurrently() -> None:
    """不同 session 必须并行（否则一个慢群会拖住所有人）。"""
    ex = SessionExecutor()
    events: list[str] = []

    async def slow() -> None:
        events.append("slow:start")
        await asyncio.sleep(0.05)
        events.append("slow:end")

    async def fast() -> None:
        events.append("fast:start")
        await asyncio.sleep(0.01)
        events.append("fast:end")

    await ex.submit("s1", slow)
    await ex.submit("s2", fast)
    await ex.drain()

    # fast 必须在 slow 结束之前就开始 → 证明并行
    assert events.index("fast:start") < events.index("slow:end")


@pytest.mark.asyncio
async def test_fifo_order_within_session() -> None:
    ex = SessionExecutor()
    order: list[int] = []

    async def task(n: int) -> None:
        order.append(n)

    for i in range(5):
        await ex.submit("s", (lambda n: (lambda: task(n)))(i))
    await ex.drain()
    assert order == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_backpressure_rejects_newest() -> None:
    """队列满时拒绝最新任务（默认策略）。

    注意计数语义：`qsize()` 统计的是**已入队但尚未被 worker 取走**的任务。
    worker 是异步的，因此刚 submit 完时它还没来得及取走第一个，
    队列长度会短暂包含"即将开始执行"的那个。这是正常现象——
    背压看的是"积压量"，不是"正在执行量"。
    """
    ex = SessionExecutor(max_queue_size=2)
    release = asyncio.Event()
    started: list[int] = []

    async def blocking(n: int) -> None:
        started.append(n)
        await release.wait()

    r0 = await ex.submit("s", lambda: blocking(0))
    assert r0.accepted is True
    r1 = await ex.submit("s", lambda: blocking(1))
    assert r1.accepted is True

    # 此时队列已有 2 个（容量 2）→ 第 3 个被拒
    r2 = await ex.submit("s", lambda: blocking(2))
    assert r2.accepted is False
    assert r2.reject_reason == "queue_full"

    release.set()
    await ex.drain()
    # 被拒绝的任务绝不能被执行
    assert 2 not in started


@pytest.mark.asyncio
async def test_backpressure_isolated_per_session() -> None:
    """一个 session 满了不该影响另一个 session。"""
    ex = SessionExecutor(max_queue_size=1)
    release = asyncio.Event()

    async def blocking() -> None:
        await release.wait()

    assert (await ex.submit("busy", blocking)).accepted is True
    assert (await ex.submit("busy", blocking)).accepted is False
    # 另一个 session 不受影响
    assert (await ex.submit("free", blocking)).accepted is True
    release.set()
    await ex.drain()


@pytest.mark.asyncio
async def test_task_failure_does_not_break_queue() -> None:
    """单个任务抛异常，后续任务仍要执行。"""
    ex = SessionExecutor()
    done: list[str] = []

    async def boom() -> None:
        raise RuntimeError("task boom")

    async def ok() -> None:
        done.append("ok")

    await ex.submit("s", boom)
    await ex.submit("s", ok)
    await ex.drain()
    assert done == ["ok"]
    assert ex.stats()["failed"] == 1


@pytest.mark.asyncio
async def test_run_returns_result() -> None:
    ex = SessionExecutor()

    async def task() -> int:
        return 7

    assert await ex.run("s", task) == 7


@pytest.mark.asyncio
async def test_run_propagates_exception() -> None:
    ex = SessionExecutor()

    async def boom() -> None:
        raise ValueError("nope")

    with pytest.raises(ValueError):
        await ex.run("s", boom)


@pytest.mark.asyncio
async def test_run_raises_on_full_queue() -> None:
    ex = SessionExecutor(max_queue_size=1)
    release = asyncio.Event()

    async def blocking() -> None:
        await release.wait()

    await ex.submit("s", blocking)
    await ex.submit("s", blocking)
    with pytest.raises(QueueFullError):
        await ex.run("s", blocking)
    release.set()
    await ex.drain()


@pytest.mark.asyncio
async def test_close_rejects_new_tasks() -> None:
    ex = SessionExecutor()
    await ex.close()
    result = await ex.submit("s", lambda: asyncio.sleep(0))
    assert result.accepted is False
    assert result.reject_reason == "closed"


@pytest.mark.asyncio
async def test_reject_callback_invoked() -> None:
    seen: list[tuple[str, str]] = []
    ex = SessionExecutor(max_queue_size=1, on_reject=lambda k, r: seen.append((k, r)))
    release = asyncio.Event()

    async def blocking() -> None:
        await release.wait()

    await ex.submit("s", blocking)   # 入队，占满
    await ex.submit("s", blocking)   # 应被拒
    assert seen == [("s", "queue_full")]
    release.set()
    await ex.drain()


@pytest.mark.asyncio
async def test_stats_tracking() -> None:
    ex = SessionExecutor()

    async def ok() -> None:
        return None

    await ex.submit("s", ok)
    await ex.drain()
    stats = ex.stats()
    assert stats["submitted"] == 1
    assert stats["completed"] == 1


@pytest.mark.asyncio
async def test_queue_size_observable() -> None:
    ex = SessionExecutor()
    release = asyncio.Event()

    async def blocking() -> None:
        await release.wait()

    await ex.submit("s", blocking)
    await ex.submit("s", blocking)
    # 两个都在队列中（worker 尚未取走）
    assert ex.queue_size("s") == 2
    assert ex.total_queued() >= 2
    release.set()
    await ex.drain()
    # 排空后归零
    assert ex.queue_size("s") == 0


# ================================================================
# Session key：记忆作用域 ≠ 并发作用域
# ================================================================


def test_agent_key_differs_from_memory_scope() -> None:
    """Agent 并发键必须按人隔离，记忆作用域按群共享。"""
    from dl_senpai.gateway.context import agent_session_key, MessageContext

    ctx = MessageContext(
        message_type="group", group_id="100", user_id="7", session_id="group:100"
    )
    # 记忆作用域：整个群共享
    assert ctx.session_id == "group:100"
    # 并发作用域：按人隔离
    assert agent_session_key(ctx) == "group:100:user:7"


def test_agent_key_private() -> None:
    from dl_senpai.gateway.context import agent_session_key, MessageContext

    ctx = MessageContext(message_type="private", user_id="7", session_id="private:7")
    assert agent_session_key(ctx) == "private:7"


def test_two_users_in_same_group_get_different_keys() -> None:
    """同一个群里两个人必须能并发（否则 100 人群会互相排队）。"""
    from dl_senpai.gateway.context import agent_session_key, MessageContext

    a = MessageContext(message_type="group", group_id="100", user_id="1")
    b = MessageContext(message_type="group", group_id="100", user_id="2")
    assert agent_session_key(a) != agent_session_key(b)
    # 但记忆作用域相同（学姐要记得群上下文）
    assert a.session_id == b.session_id if a.session_id else True


# ================================================================
# Router + Observer 分离
# ================================================================


def ctx(**kw) -> RouteContext:
    base = {"text": "签到", "session_id": "group:1", "user_id": "9"}
    base.update(kw)
    return RouteContext(**base)


@pytest.mark.asyncio
async def test_observer_runs_even_when_command_consumes() -> None:
    """核心语义：命令 consume 后，旁路观察者仍要看到消息。

    这正是表情采集器的需求——用户在"签到"消息里发的表情同样要被采集。
    """
    router = Router()
    seen: list[str] = []

    router.command("cmd", lambda c: True, lambda c: RouteResult.handle(consumed=True))
    router.observe("obs", lambda c: seen.append("observed"))

    await router.dispatch_observers(ctx())
    await router.dispatch(ctx())

    assert seen == ["observed"]


@pytest.mark.asyncio
async def test_observer_never_consumes() -> None:
    """观察者即使返回 consume=True，也会被强制归零。"""
    router = Router()
    called: list[str] = []

    router.observe(
        "obs", lambda c: RouteResult.handle(consumed=True, reason="tried to consume")
    )
    router.plugin_route("plug", lambda c: True, lambda c: called.append("plug") or RouteResult.handle())
    router.observe("obs2", lambda c: called.append("obs2") or None)

    results = await router.dispatch_observers(ctx())
    assert results[0].consumed is False
    assert results[0].handled is True

    await router.dispatch(ctx())
    assert "plug" in called
    assert "obs2" in called


@pytest.mark.asyncio
async def test_observer_order_by_priority() -> None:
    router = Router()
    order: list[str] = []
    router.observe("late", lambda c: order.append("late"), priority=900)
    router.observe("early", lambda c: order.append("early"), priority=10)
    await router.dispatch_observers(ctx())
    assert order == ["early", "late"]


@pytest.mark.asyncio
async def test_observer_failure_isolated() -> None:
    router = Router()
    seen: list[str] = []

    def boom(_c):
        raise RuntimeError("observer boom")

    router.observe("bad", boom)
    router.observe("good", lambda c: seen.append("ok"))
    await router.dispatch_observers(ctx())
    assert seen == ["ok"]


@pytest.mark.asyncio
async def test_observer_unregister_by_owner() -> None:
    router = Router()
    router.observe("a", lambda c: None, owner="p1")
    router.observe("b", lambda c: None, owner="p1")
    router.observe("c", lambda c: None, owner="p2")
    assert router.unregister_observer_owner("p1") == 2
    assert [o.name for o in router.observers()] == ["c"]


@pytest.mark.asyncio
async def test_route_result_handled_vs_consumed() -> None:
    """handled 与 consumed 必须能独立表达（Phase 3 第二十节）。"""
    observed = RouteResult.observe(reason="sticker")
    assert observed.handled is True
    assert observed.consumed is False
    assert observed.replied is False

    handled = RouteResult.handle(reason="checkin", replied=True)
    assert handled.handled is True
    assert handled.consumed is True
    assert handled.replied is True

    skipped = RouteResult.skip(reason="nope")
    assert skipped.handled is False
    assert skipped.consumed is False


def test_route_result_matched_synced_with_handled() -> None:
    """兼容旧字段：只用 matched 构造时 handled 自动同步。"""
    r = RouteResult(matched=True, consumed=True)
    assert r.handled is True
    r2 = RouteResult(handled=True)
    assert r2.matched is True


@pytest.mark.asyncio
async def test_full_stage_order_with_observer() -> None:
    """完整顺序：Observer → Command → Plugin → Agent。"""
    router = Router()
    order: list[str] = []

    router.observe("obs", lambda c: order.append("observer"))
    router.agent_route("agent", lambda c: True, lambda c: order.append("agent") or RouteResult.handle())
    router.plugin_route("plug", lambda c: True, lambda c: order.append("plugin") or RouteResult.handle(consumed=False))
    router.command("cmd", lambda c: True, lambda c: order.append("command") or RouteResult.handle(consumed=False))

    await router.dispatch_observers(ctx())
    await router.dispatch(ctx())

    assert order == ["observer", "command", "plugin", "agent"]


@pytest.mark.asyncio
async def test_command_consumes_stops_agent() -> None:
    router = Router()
    order: list[str] = []
    router.command("cmd", lambda c: True, lambda c: order.append("command") or RouteResult.handle(consumed=True))
    router.agent_route("agent", lambda c: True, lambda c: order.append("agent") or RouteResult.handle())
    await router.dispatch(ctx())
    assert order == ["command"]
