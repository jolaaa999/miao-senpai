"""Session 执行器：保证「同 session 串行、不同 session 并行」。

Phase 3 任务 F。重构前每条消息独立处理，同一个群里两个人几乎同时 @学姐，
两次 LLM 调用会并发读写**同一个记忆文件**，结果是上下文交错。

设计：
    session_key → FIFO 队列 → 单 worker 串行消费
    不同 session_key 各自有 worker，天然并行

Backpressure（Phase 3 任务二十六）：默认 **reject newest**。
同一用户狂刷时不能无限积压（每个排队任务都持有事件对象与 bot 引用，
不设上限会把内存吃干）。被拒绝的任务返回明确结果并记日志，
由调用方决定是否回一句"别急"。

为什么不 drop oldest：群里刷屏的场景下，最新的消息才是用户真正想说的，
丢掉它去处理一条 10 秒前的旧消息不合理。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable

TaskFn = Callable[[], Awaitable[Any]]


def _current_telemetry() -> Any:
    """取当前请求的 telemetry（Phase 7A.1）。

    延迟 import + 静默失败：观测不能影响队列语义。
    """
    try:
        from ...validation.telemetry import current_telemetry

        return current_telemetry()
    except Exception:  # noqa: BLE001
        return None


class Reject(str, Enum):
    """任务被拒绝的原因。"""

    QUEUE_FULL = "queue_full"
    CLOSED = "closed"


@dataclass
class SubmitResult:
    """提交结果。

    accepted=False 表示因背压被拒；此时 `queued` 是当前队列长度，
    可用于日志与用户提示。
    """

    accepted: bool
    session_key: str
    queued: int = 0
    reject_reason: str = ""


@dataclass
class _QueuedTask:
    """队列里的一个待执行任务 + 它的入队观测信息。

    Phase 7A.1：为了回填 `queue_wait` / `queue_depth`，必须记住
    **入队时刻**。原先队列里直接放裸协程，无法知道等了多久。
    """

    fn: TaskFn
    enqueued_at: float
    queue_depth_at_enqueue: int
    telemetry: Any = None


@dataclass
class _SessionQueue:
    """单个 session 的串行队列。"""

    key: str
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    worker: asyncio.Task | None = None
    running: bool = False


class SessionExecutor:
    """按 session 串行执行异步任务。

    Args:
        max_queue_size: 单 session 队列上限（不含正在执行的那个）。
        on_reject: 被拒绝时的回调（用于日志/提示）。
    """

    def __init__(
        self,
        *,
        max_queue_size: int = 8,
        on_reject: Callable[[str, str], None] | None = None,
    ) -> None:
        self._sessions: dict[str, _SessionQueue] = {}
        self._max_queue_size = max(1, int(max_queue_size))
        self._on_reject = on_reject
        self._closed = False
        self._lock = asyncio.Lock()
        # 统计（供 observability / 测试断言）
        self._stats = {
            "submitted": 0,
            "rejected": 0,
            "completed": 0,
            "failed": 0,
        }

    # -- 提交 --

    async def submit(self, session_key: str, task: TaskFn) -> SubmitResult:
        """提交任务。返回是否被接受（不等待执行完成）。

        Phase 7A.1 插桩：记录入队时刻与当时的队列深度，
        供 worker 计算 `queue_wait`。**业务顺序完全不变**。
        """
        if self._closed:
            return SubmitResult(False, session_key, 0, Reject.CLOSED.value)

        async with self._lock:
            session = self._sessions.get(session_key)
            if session is None:
                session = _SessionQueue(key=session_key)
                self._sessions[session_key] = session

            # 背压：队列已满则拒绝最新任务
            if session.queue.qsize() >= self._max_queue_size:
                self._stats["rejected"] += 1
                # 第九/十一节：拒绝时必须留下可关联的观测数据
                self._record_reject(
                    session_key, session.queue.qsize(), Reject.QUEUE_FULL.value
                )
                if self._on_reject is not None:
                    try:
                        self._on_reject(session_key, Reject.QUEUE_FULL.value)
                    except Exception:  # noqa: BLE001
                        pass
                return SubmitResult(
                    False, session_key, session.queue.qsize(), Reject.QUEUE_FULL.value
                )

            # queue_depth 的定义：**不含当前请求**的等待数。
            # 即"我入队前队列里已经有几个"。
            # 这样语义明确且可测：空队列提交时 depth=0。
            depth_before = session.queue.qsize()
            session.queue.put_nowait(
                _QueuedTask(
                    fn=task,
                    enqueued_at=time.monotonic(),
                    queue_depth_at_enqueue=depth_before,
                    telemetry=_current_telemetry(),
                )
            )
            self._stats["submitted"] += 1
            queued = session.queue.qsize()
            self._ensure_worker(session)
            return SubmitResult(True, session_key, queued)

    def _record_reject(self, session_key: str, depth: int, reason: str) -> None:
        """记录一次背压拒绝（第十一节）。**不记录原始 session id。**"""
        try:
            from ...validation.privacy import anonymize
            from ...validation.telemetry import current_telemetry

            tel = _current_telemetry()
            if tel is not None:
                tel.rejected = True
                tel.reject_reason = reason
                tel.queue_depth = depth
                tel.error_class = "queue_rejected"

            from ...observability.logging import log_event

            log_event(
                "session.rejected",
                level="info",  # 第三十一节：背压是正常业务事件
                session_hash=anonymize(session_key, kind="session"),
                queue_depth=depth,
                reason=reason,
            )
        except Exception:  # noqa: BLE001 — 观测失败不影响背压行为
            pass

    async def run(self, session_key: str, task: TaskFn) -> Any:
        """提交并**等待**任务完成。主要用于测试与需要结果调用方的场景。"""
        done: asyncio.Future = asyncio.get_running_loop().create_future()

        async def wrapper() -> None:
            try:
                result = await task()
                if not done.done():
                    done.set_result(result)
            except Exception as exc:  # noqa: BLE001
                if not done.done():
                    done.set_exception(exc)
                raise

        result = await self.submit(session_key, wrapper)
        if not result.accepted:
            raise QueueFullError(session_key, result.reject_reason)
        return await done

    # -- worker --

    def _ensure_worker(self, session: _SessionQueue) -> None:
        """确保该 session 有 worker 在跑（惰性创建）。"""
        if session.worker is not None and not session.worker.done():
            return
        session.worker = asyncio.create_task(self._worker(session))

    async def _worker(self, session: _SessionQueue) -> None:
        session.running = True
        try:
            while True:
                try:
                    item = session.queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                # Phase 7A.1：queue_wait = 入队 → 真正开始执行 的间隔。
                # 这正是第八节要求的定义（不是整个请求 latency）。
                started_at = time.monotonic()
                self._instrument_start(item, session, started_at)
                try:
                    await item.fn()
                    self._stats["completed"] += 1
                except Exception as exc:  # noqa: BLE001 — 单个任务失败不拖垮队列
                    self._stats["failed"] += 1
                    self._log_failure(session.key, exc)
                finally:
                    self._instrument_finish(item, started_at)
                    session.queue.task_done()
        finally:
            session.running = False

    def _instrument_start(
        self, item: _QueuedTask, session: _SessionQueue, started_at: float
    ) -> None:
        """把队列等待与并发度写进该请求的 telemetry。**

        fail-open：任何异常都不影响任务执行。
        """
        try:
            tel = item.telemetry
            if tel is None:
                return
            tel.queue_wait_ms = (started_at - item.enqueued_at) * 1000
            tel.queue_depth = item.queue_depth_at_enqueue
            tel.active_sessions = self.active_sessions()
            if not tel.accepted_at:
                tel.accepted_at = item.enqueued_at
        except Exception:  # noqa: BLE001
            pass

    def _instrument_finish(self, item: _QueuedTask, started_at: float) -> None:
        try:
            tel = item.telemetry
            if tel is not None:
                tel.finished_at = time.monotonic()
        except Exception:  # noqa: BLE001
            pass

    def _log_failure(self, session_key: str, exc: BaseException) -> None:
        try:
            from ...observability.logging import log_event

            log_event(
                "session.task_failed",
                level="warning",
                session_key=session_key,
                error=f"{type(exc).__name__}: {exc}",
            )
        except Exception:  # noqa: BLE001
            pass

    # -- 生命周期 --

    async def drain(self, timeout: float = 10.0) -> bool:
        """等待所有队列排空、所有 worker 结束。返回是否在超时内完成。

        实现说明：`asyncio.wait` 只接受 Task/Future，**不接受协程**，
        因此 `queue.join()` 必须先 `ensure_future` 包装。
        循环等待是因为"队列空了"与"worker 真的退出"是两个时刻——
        worker 可能在 `task_done()` 之后还有收尾代码。
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            workers = [
                s.worker
                for s in self._sessions.values()
                if s.worker is not None and not s.worker.done()
            ]
            pending_queues = [
                s.queue for s in self._sessions.values() if not s.queue.empty()
            ]
            if not workers and not pending_queues:
                return True

            remaining = deadline - loop.time()
            if remaining <= 0:
                return False

            waiters: list[asyncio.Future] = []
            waiters.extend(asyncio.ensure_future(w) for w in workers)
            for q in pending_queues:
                waiters.append(asyncio.ensure_future(q.join()))
            if not waiters:
                return True
            try:
                await asyncio.wait(waiters, timeout=remaining)
            finally:
                for w in waiters:
                    if not w.done():
                        w.cancel()

    async def close(self) -> None:
        """停止接收新任务并取消所有 worker。"""
        self._closed = True
        async with self._lock:
            workers = [
                s.worker
                for s in self._sessions.values()
                if s.worker is not None and not s.worker.done()
            ]
        for w in workers:
            w.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._sessions.clear()

    # -- 观测 --

    def queue_size(self, session_key: str) -> int:
        session = self._sessions.get(session_key)
        return session.queue.qsize() if session is not None else 0

    def total_queued(self) -> int:
        return sum(s.queue.qsize() for s in self._sessions.values())

    def active_sessions(self) -> int:
        return sum(
            1
            for s in self._sessions.values()
            if s.worker is not None and not s.worker.done()
        )

    def stats(self) -> dict[str, int]:
        return dict(self._stats)


class QueueFullError(RuntimeError):
    """session 队列已满（背压拒绝）。"""

    def __init__(self, session_key: str, reason: str = "") -> None:
        super().__init__(f"session {session_key} 队列已满: {reason}")
        self.session_key = session_key
        self.reason = reason


# 全局单例
_executor: SessionExecutor | None = None


def get_executor() -> SessionExecutor:
    global _executor
    if _executor is None:
        _executor = SessionExecutor()
    return _executor


def reset_executor(executor: SessionExecutor | None = None) -> None:
    """替换全局执行器（测试用）。"""
    global _executor
    _executor = executor


__all__ = [
    "SessionExecutor",
    "SubmitResult",
    "QueueFullError",
    "Reject",
    "get_executor",
    "reset_executor",
]
