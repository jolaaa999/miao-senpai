"""RequestTelemetry：一次请求的观测数据轻量载体（Phase 7A.1 第五/六节）。

**为什么需要它**：
    第五节点名了问题 —— SessionExecutor、ToolExecutor、MemoryFacade、
    LLM、reply_send 各拼一个 dict，最后没法关联也没法保证字段一致。

    这个对象是**唯一**的观测数据容器：各层只往里写自己那一段，
    ValidationRecorder 最后读它一次落盘。

**它不是什么**：
    - 不是 Service Locator（不持有 facade / executor / config）
    - 不是业务对象（业务代码不该读它做决策）
    - 不是全局单例（**每次请求一个**，通过 contextvar 传递）

**为什么用 contextvar 而不是显式传参**：
    ToolExecutor / MemoryFacade / reply_send 的调用签名已被大量测试与
    生产代码固定（例如 `executor.execute(call, context, dry_run=...)`）。
    为了插桩而给这些签名加参数会波及几十个调用点，风险远大于收益。
    contextvar 在 asyncio 任务内自动继承，正是"隐式上下文"的合适场景 ——
    与项目既有的 trace_id 机制（observability 层）同源。

**Fail-open**：所有写入方法都吞异常。观测失败绝不影响业务。
"""

from __future__ import annotations

import contextvars
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ================================================================
# 工具执行结果（第十四节：与 ToolResult/Journal 语义对齐）
# ================================================================


class ToolOutcome(str, Enum):
    """工具执行的终态。

    与既有语义对齐（**不创建矛盾的新状态体系**）：
        SUCCESS           —— ToolResult.success=True
        FAILURE           —— ToolResult.success=False 且非超时
        TIMEOUT           —— error_type == "timeout"
        PERMISSION_DENIED —— 权限层拒绝（执行前）
        CANCELLED         —— asyncio.CancelledError
        DRY_RUN           —— shadow/演练模式，**未真正执行**
        SKIPPED           —— journal 幂等命中，未执行
    """

    SUCCESS = "success"
    FAILURE = "failure"
    TIMEOUT = "timeout"
    PERMISSION_DENIED = "permission_denied"
    CANCELLED = "cancelled"
    DRY_RUN = "dry_run"
    SKIPPED = "skipped"


# 终态集合：同一 tool_call_id 只能出现一次（第十八节 No Double Instrumentation）
TERMINAL_OUTCOMES = frozenset(
    {
        ToolOutcome.SUCCESS,
        ToolOutcome.FAILURE,
        ToolOutcome.TIMEOUT,
        ToolOutcome.PERMISSION_DENIED,
        ToolOutcome.CANCELLED,
        ToolOutcome.DRY_RUN,
        ToolOutcome.SKIPPED,
    }
)


@dataclass
class ToolTelemetry:
    """单个工具调用的一次终态记录。"""

    tool: str
    call_id: str = ""
    outcome: str = ""
    # 计时口径（第十五节）：真正执行 executor → 返回/超时/异常。
    # 权限检查耗时**不含**在内（单列 permission_ms）。
    duration_ms: float = 0.0
    permission_ms: float = 0.0
    dry_run: bool = False
    permission_denied: bool = False
    visibility_denied: bool = False
    error_type: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "call_id": self.call_id,
            "outcome": self.outcome,
            "duration_ms": round(self.duration_ms, 2),
            "permission_ms": round(self.permission_ms, 2),
            "dry_run": self.dry_run,
            "permission_denied": self.permission_denied,
            "visibility_denied": self.visibility_denied,
            "error_type": self.error_type,
        }


@dataclass
class ModelCallTelemetry:
    """单次模型调用的记录（第二十九节）。"""

    model: str = ""
    provider: str = ""
    call_index: int = 0
    latency_ms: float = 0.0
    # 第三十节：provider 不返回 token 时保持 None，**不估算冒充**
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    # 若由本地 tokenizer 精确计算，标 True；不混为 provider-reported
    estimated: bool = False
    status: str = ""
    retry_count: int = 0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "call_index": self.call_index,
            "latency_ms": round(self.latency_ms, 2),
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "estimated": self.estimated,
            "status": self.status,
            "retry_count": self.retry_count,
            "error": self.error,
        }


@dataclass
class SendTelemetry:
    """发送侧记录（第三十八节）。

    **区分 response 与 send operation**（第三十九节）：
        一次 Agent response 可能因为多气泡/碎句触发多次 QQ send，
        因此 `send_operations` 与 `bubbles` 会大于 1，但
        `responses` 仍是 1。
    """

    responses: int = 0
    send_operations: int = 0
    bubbles: int = 0
    send_success: int = 0
    send_failure: int = 0
    retry: int = 0
    forward_message: int = 0
    media_send_failure: int = 0
    fallback_text: int = 0
    total_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "responses": self.responses,
            "send_operations": self.send_operations,
            "bubbles": self.bubbles,
            "send_success": self.send_success,
            "send_failure": self.send_failure,
            "retry": self.retry,
            "forward_message": self.forward_message,
            "media_send_failure": self.media_send_failure,
            "fallback_text": self.fallback_text,
            "total_ms": round(self.total_ms, 2),
        }


@dataclass
class MemoryTelemetry:
    """MemoryFacade 读取统计（第二十节：**不含内容**）。"""

    history_count: int = 0
    recalled_count: int = 0
    person_profile_hit: bool = False
    group_style_hit: bool = False
    affection_loaded: bool = False
    inner_state_loaded: bool = False
    trends_loaded: bool = False
    scope_violations: int = 0
    scope_reason: str = ""
    total_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "history_count": self.history_count,
            "recalled_count": self.recalled_count,
            "person_profile_hit": self.person_profile_hit,
            "group_style_hit": self.group_style_hit,
            "affection_loaded": self.affection_loaded,
            "inner_state_loaded": self.inner_state_loaded,
            "trends_loaded": self.trends_loaded,
            "scope_violation": self.scope_violations > 0,
            "scope_reason": self.scope_reason,
            "total_ms": round(self.total_ms, 2),
        }


# ================================================================
# RequestTelemetry
# ================================================================


@dataclass
class RequestTelemetry:
    """一次请求的完整观测数据。

    各层只写自己的字段；ValidationRecorder 最后统一读取。
    """

    trace_id: str = ""

    # -- 队列（第七~十一节）--
    # 定义见 SessionExecutor 插桩处
    queue_wait_ms: float = 0.0
    queue_depth: int = 0
    active_sessions: int = 0
    rejected: bool = False
    reject_reason: str = ""

    # -- 路由 --
    routing_ms: float = 0.0

    # -- 阶段累计 --
    memory_ms: float = 0.0
    model_ms: float = 0.0
    tool_ms: float = 0.0
    response_ms: float = 0.0
    send_ms: float = 0.0

    # -- 明细 --
    tools: list[ToolTelemetry] = field(default_factory=list)
    model_calls: list[ModelCallTelemetry] = field(default_factory=list)
    send: SendTelemetry = field(default_factory=SendTelemetry)
    memory: MemoryTelemetry = field(default_factory=MemoryTelemetry)

    # -- 错误 --
    error_class: str = ""
    error_detail: str = ""

    # -- 计时锚点 --
    accepted_at: float = 0.0
    finished_at: float = 0.0

    # 去重集合：同一 tool_call_id 只允许一条终态（第十八节）
    _terminal_ids: set[str] = field(default_factory=set, repr=False)
    _lock: Any = field(default_factory=threading.Lock, repr=False)

    # -- 写入接口（全部 fail-open）--

    def record_tool(self, item: ToolTelemetry) -> bool:
        """记录一条工具终态。返回是否被接受（重复 id 返回 False）。"""
        try:
            with self._lock:
                if item.call_id and item.call_id in self._terminal_ids:
                    return False  # 幂等：同一 call 只记一条终态
                if item.call_id:
                    self._terminal_ids.add(item.call_id)
                self.tools.append(item)
                self.tool_ms += item.duration_ms
            return True
        except Exception:  # noqa: BLE001 — 观测失败不影响业务
            return False

    def record_model_call(self, item: ModelCallTelemetry) -> bool:
        try:
            with self._lock:
                item.call_index = len(self.model_calls) + 1
                self.model_calls.append(item)
                self.model_ms += item.latency_ms
            return True
        except Exception:  # noqa: BLE001
            return False

    def record_memory(
        self,
        *,
        history_count: int = 0,
        recalled_count: int = 0,
        person_profile_hit: bool = False,
        group_style_hit: bool = False,
        affection_loaded: bool = False,
        inner_state_loaded: bool = False,
        trends_loaded: bool = False,
        duration_ms: float = 0.0,
    ) -> None:
        try:
            with self._lock:
                m = self.memory
                m.history_count = max(m.history_count, int(history_count))
                m.recalled_count = max(m.recalled_count, int(recalled_count))
                m.person_profile_hit |= bool(person_profile_hit)
                m.group_style_hit |= bool(group_style_hit)
                m.affection_loaded |= bool(affection_loaded)
                m.inner_state_loaded |= bool(inner_state_loaded)
                m.trends_loaded |= bool(trends_loaded)
                m.total_ms += duration_ms
                self.memory_ms += duration_ms
        except Exception:  # noqa: BLE001
            pass

    def record_scope_violation(self, reason: str) -> None:
        """记录跨作用域违规（第二十四节，CRITICAL）。"""
        try:
            with self._lock:
                self.memory.scope_violations += 1
                self.memory.scope_reason = reason
        except Exception:  # noqa: BLE001
            pass

    def record_send(
        self,
        *,
        success: bool = True,
        operations: int = 1,
        bubbles: int = 1,
        retry: int = 0,
        forward: bool = False,
        media_failure: bool = False,
        fallback: bool = False,
        duration_ms: float = 0.0,
    ) -> None:
        try:
            with self._lock:
                s = self.send
                s.responses += 1
                s.send_operations += operations
                s.bubbles += bubbles
                s.retry += retry
                if forward:
                    s.forward_message += 1
                if media_failure:
                    s.media_send_failure += 1
                if fallback:
                    s.fallback_text += 1
                if success:
                    s.send_success += operations
                else:
                    s.send_failure += operations
                s.total_ms += duration_ms
                self.send_ms += duration_ms
        except Exception:  # noqa: BLE001
            pass

    def record_error(self, error_class: str, detail: str = "") -> None:
        try:
            self.error_class = error_class
            self.error_detail = detail[:300]
        except Exception:  # noqa: BLE001
            pass

    # -- 派生 --

    @property
    def model_call_count(self) -> int:
        return len(self.model_calls)

    @property
    def total_tokens(self) -> int:
        """总 token。

        **只累加 provider-reported 的非 None 值**（第三十节）：
        某个 provider 不返回 token 时，不把 None 当 0 静默吞掉，
        也不估算 —— 而是让总数只反映已知部分。
        """
        total = 0
        for call in self.model_calls:
            if call.total_tokens is not None:
                total += call.total_tokens
        return total

    @property
    def any_token_unknown(self) -> bool:
        """是否有调用未返回 token（供报告标注数据完整性）。"""
        return any(c.total_tokens is None for c in self.model_calls)

    @property
    def tool_calls(self) -> list[str]:
        """真正的工具调用（不含 DRY_RUN/SKIPPED —— 它们未执行）。"""
        return [
            t.tool
            for t in self.tools
            if t.outcome not in (ToolOutcome.DRY_RUN, ToolOutcome.SKIPPED)
        ]

    @property
    def latency_stages(self) -> dict[str, float]:
        """分阶段延迟（第四十三节）。

        **不保证可加**（第四十六节）：阶段可能重叠（例如 memory 读取
        发生在 model 调用内部），因此报告需注明这些是诊断值而非累加项。
        """
        return {
            "queue_wait": round(self.queue_wait_ms, 2),
            "routing": round(self.routing_ms, 2),
            "memory": round(self.memory_ms, 2),
            "model": round(self.model_ms, 2),
            "tool": round(self.tool_ms, 2),
            "response": round(self.response_ms, 2),
            "send": round(self.send_ms, 2),
        }

    def total_latency_ms(self, *, include_send: bool = True) -> float:
        """端到端耗时（第四十五节）。

        定义：请求被接受 → response pipeline 完成。
        `include_send=True` 时把发送也算进去（明确命名语义）。
        """
        if self.accepted_at and self.finished_at:
            total = (self.finished_at - self.accepted_at) * 1000
        else:
            stages = self.latency_stages
            total = sum(
                v for k, v in stages.items()
                if include_send or k != "send"
            )
        return round(total, 2)

    def as_dict(self) -> dict[str, Any]:
        return {
            "queue_wait": round(self.queue_wait_ms, 2),
            "queue_depth": self.queue_depth,
            "active_sessions": self.active_sessions,
            "rejected": self.rejected,
            "reject_reason": self.reject_reason,
            "latency": {
                **self.latency_stages,
                "total": self.total_latency_ms(),
            },
            "tools": [t.as_dict() for t in self.tools],
            "model_calls": [c.as_dict() for c in self.model_calls],
            "model_call_count": self.model_call_count,
            "tokens": {
                "total": self.total_tokens,
                "unknown": self.any_token_unknown,
            },
            "memory": self.memory.as_dict(),
            "send": self.send.as_dict(),
            "error_class": self.error_class,
            "error_detail": self.error_detail,
        }


# ================================================================
# contextvar 载体
# ================================================================

_current: contextvars.ContextVar[RequestTelemetry | None] = contextvars.ContextVar(
    "dl_senpai_request_telemetry", default=None
)


def current_telemetry() -> RequestTelemetry | None:
    """取当前上下文的 telemetry（可能为 None —— 未插桩路径）。"""
    return _current.get()


def bind_telemetry(tel: RequestTelemetry) -> contextvars.Token:
    """绑定当前请求的 telemetry。返回 token 供 reset。"""
    return _current.set(tel)


def unbind_telemetry(token: contextvars.Token) -> None:
    try:
        _current.reset(token)
    except Exception:  # noqa: BLE001 — token 跨上下文 reset 会报错，忽略
        pass


def record_tool_call(item: ToolTelemetry) -> None:
    """便捷入口：往当前 telemetry 写工具终态（无 telemetry 时静默跳过）。"""
    tel = _current.get()
    if tel is not None:
        tel.record_tool(item)


def record_model_call(item: ModelCallTelemetry) -> None:
    tel = _current.get()
    if tel is not None:
        tel.record_model_call(item)


def record_send(**kwargs: Any) -> None:
    tel = _current.get()
    if tel is not None:
        tel.record_send(**kwargs)


def record_memory(**kwargs: Any) -> None:
    tel = _current.get()
    if tel is not None:
        tel.record_memory(**kwargs)


def record_scope_violation(reason: str) -> None:
    tel = _current.get()
    if tel is not None:
        tel.record_scope_violation(reason)


__all__ = [
    "ToolOutcome",
    "TERMINAL_OUTCOMES",
    "ToolTelemetry",
    "ModelCallTelemetry",
    "SendTelemetry",
    "MemoryTelemetry",
    "RequestTelemetry",
    "current_telemetry",
    "bind_telemetry",
    "unbind_telemetry",
    "record_tool_call",
    "record_model_call",
    "record_send",
    "record_memory",
    "record_scope_violation",
]
