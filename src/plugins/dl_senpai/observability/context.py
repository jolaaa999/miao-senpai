"""Trace 上下文：把一次 QQ 消息生命周期串成一条可追踪的链路。

问题背景：重构前一条群消息会经过 7 个 NoneBot matcher → `_handle_chat`
→ LLM → 5 类副作用，日志全部是 `f"dl_senpai LLM ok session=..."` 这样的
手工拼接。同一条消息产生的十几行日志之间**没有任何标识可以关联**，
排查「这条消息为什么没回」只能靠时间戳猜。

本模块用 `contextvars` 提供协程安全的隐式上下文：

    with trace_scope(session_id="group:123", message_id="456"):
        ...
        trace_id()   # 整条链路同一个值

为什么用 contextvars 而不是显式传参：现有 47 个模块的函数签名已经很长
（`llm.chat()` 有 20 个关键字参数），逐个加 `trace_id` 参数会污染所有
接口。contextvars 在 asyncio 下按「任务」隔离，不同群的消息并发处理时
互不串台。
"""

from __future__ import annotations

import contextvars
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

# 当前 trace 上下文；None 表示不在任何追踪范围内（例如后台循环、单测）
_current: contextvars.ContextVar["TraceContext | None"] = contextvars.ContextVar(
    "dl_senpai_trace", default=None
)


@dataclass
class TraceContext:
    """一次消息处理的追踪上下文。"""

    trace_id: str
    session_id: str = ""
    message_id: str = ""
    # 当前阶段名（gateway/router/agent/tool/reply），用于日志分组统计
    stage: str = "init"
    # 任意附加维度：群号、用户号、事件类型等
    fields: dict[str, Any] = field(default_factory=dict)
    started_at: float = field(default_factory=time.perf_counter)

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started_at) * 1000.0

    def as_log_fields(self) -> dict[str, Any]:
        """摊平成日志字段；只带非空值，避免日志里出现一堆空串。"""
        out: dict[str, Any] = {"trace_id": self.trace_id}
        if self.session_id:
            out["session_id"] = self.session_id
        if self.message_id:
            out["message_id"] = self.message_id
        if self.stage:
            out["stage"] = self.stage
        out.update(self.fields)
        return out


def new_trace_id() -> str:
    """生成 12 位十六进制 trace id。

    故意不用完整 uuid4：日志里出现 36 字符的 id 会显著降低肉眼扫描效率，
    12 位（48 bit）在本项目的量级下碰撞概率可以忽略。
    """
    return uuid.uuid4().hex[:12]


def current_trace() -> TraceContext | None:
    """取当前上下文；不在追踪范围内返回 None。"""
    return _current.get()


def trace_id() -> str:
    """取当前 trace_id；无上下文时返回空串（而不是抛异常）。

    为什么返回空串而不抛：日志调用遍布各处，让「没有 trace」变成需要
    try/except 的场景会得不偿失。空串在日志里表现为该字段缺失。
    """
    ctx = _current.get()
    return ctx.trace_id if ctx is not None else ""


def bind_trace(ctx: TraceContext) -> contextvars.Token:
    return _current.set(ctx)


def reset_trace(token: contextvars.Token) -> None:
    _current.reset(token)


@contextmanager
def trace_scope(
    *,
    session_id: str = "",
    message_id: str = "",
    stage: str = "init",
    trace_id_value: str | None = None,
    **fields: Any,
) -> Iterator[TraceContext]:
    """开一个追踪作用域。

    支持嵌套：内层会继承外层的 trace_id（同一轮消息的所有阶段共享一个 id），
    但可以覆盖 stage / fields。
    """
    parent = _current.get()
    if trace_id_value:
        tid = trace_id_value
    elif parent is not None:
        tid = parent.trace_id
    else:
        tid = new_trace_id()

    merged: dict[str, Any] = {}
    if parent is not None:
        merged.update(parent.fields)
    merged.update(fields)

    ctx = TraceContext(
        trace_id=tid,
        session_id=session_id or (parent.session_id if parent else ""),
        message_id=message_id or (parent.message_id if parent else ""),
        stage=stage,
        fields=merged,
    )
    token = bind_trace(ctx)
    try:
        yield ctx
    finally:
        reset_trace(token)


@contextmanager
def trace_stage(stage: str, **fields: Any) -> Iterator[TraceContext | None]:
    """在现有 trace 内切换阶段；没有外层 trace 时退化为空操作。

    这样业务代码可以无脑写 `with trace_stage("llm.request"): ...`，
    在单测/后台任务等无追踪场景下不会产生任何副作用。
    """
    parent = _current.get()
    if parent is None:
        yield None
        return
    merged = dict(parent.fields)
    merged.update(fields)
    ctx = TraceContext(
        trace_id=parent.trace_id,
        session_id=parent.session_id,
        message_id=parent.message_id,
        stage=stage,
        fields=merged,
        started_at=parent.started_at,
    )
    token = bind_trace(ctx)
    try:
        yield ctx
    finally:
        reset_trace(token)
