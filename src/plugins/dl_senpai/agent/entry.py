"""AgentEntry：生产 Agent 请求的**唯一入口**。

Phase 5 第九/十节。这个模块的存在是为了解决一个具体的架构问题：

    ❌ 禁止：Router 里出现
        if runtime_failed:
            await _handle_chat_inner(...)

    ✅ 允许：Router → AgentEntry → (Runtime | Legacy | Shadow | Canary)

Router 只知道"把消息交给 AgentEntry"。执行模式、fallback 判定、
副作用安全闸门全部封装在这一层内部。

调用链：

    Router Agent Stage
        ↓
    SessionExecutor（并发控制，见 legacy_bridge）
        ↓
    AgentEntry.handle(context)
        ├─ 解析执行模式（LEGACY/SHADOW/CANARY/RUNTIME）
        ├─ LEGACY  → legacy handler
        ├─ SHADOW  → legacy 执行 + runtime 只算
        ├─ CANARY  → 按群白名单选择
        └─ RUNTIME → runtime 执行，失败且**无副作用**时 fallback legacy
        ↓
    AgentOutcome
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from ..observability.logging import log_event
from ..observability.metrics import metrics
from .context import AgentContext, AgentResult
from .journal import ExecutionJournal
from .mode import (
    AgentExecutionMode,
    StopReason,
    mode_for_group,
    resolve_mode,
)


@dataclass
class AgentOutcome:
    """AgentEntry 的产出。

    不管走哪条路径，上层拿到的都是这个统一形状——
    这保证 ResponsePipeline 不需要知道执行模式。
    """

    handled: bool = False
    result: AgentResult | None = None
    mode: str = ""
    used_runtime: bool = False
    fallback_used: bool = False
    # legacy 路径的原始 ChatResult（供 legacy 后处理使用）
    legacy_result: Any = None
    stop_reason: str = ""
    error: str = ""
    journal: Any = None

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "used_runtime": self.used_runtime,
            "fallback_used": self.fallback_used,
            "stop_reason": self.stop_reason or None,
            "handled": self.handled,
            "error": self.error or None,
        }


class AgentEntry:
    """Agent 执行入口与 fallback 边界。

    Args:
        runtime: `AgentRuntime`（可能为 None——Agent 层构建失败时）。
        legacy_handler: 旧路径的可调用对象 `(ctx) -> AgentResult|None`。
        mode_resolver: 模式解析函数（测试可注入）。
    """

    def __init__(
        self,
        *,
        runtime: Any = None,
        legacy_handler: Any = None,
        mode_resolver: Any = None,
    ) -> None:
        self._runtime = runtime
        self._legacy_handler = legacy_handler
        self._mode_resolver = mode_resolver or resolve_mode

    # -- 模式 --

    def resolve(self, context: AgentContext) -> AgentExecutionMode:
        base = self._mode_resolver()
        return mode_for_group(base, context.group_id or None)

    # -- 主入口 --

    async def handle(self, context: AgentContext) -> AgentOutcome:
        """处理一次 Agent 请求。

        Phase 7A.1：在这里绑定 `RequestTelemetry`（第五/六节）——
        各层（SessionExecutor / ToolExecutor / MemoryFacade / LLM /
        reply_send）通过 contextvar 自动找到它，无需改各自签名。

        绑定范围覆盖整个 handle()，因此**子任务自动继承**
        （asyncio 会复制当前 context）。
        """
        from ..validation.telemetry import RequestTelemetry, bind_telemetry, unbind_telemetry

        telemetry = RequestTelemetry(trace_id=context.trace_id or "")
        token = bind_telemetry(telemetry)
        try:
            outcome = await self._handle_inner(context)
        finally:
            unbind_telemetry(token)
        return outcome

    async def _handle_inner(self, context: AgentContext) -> AgentOutcome:
        """handle 的实际实现（telemetry 已绑定）。"""
        mode = self.resolve(context)
        outcome = AgentOutcome(mode=mode.value)

        metrics.incr("agent.entry")
        metrics.incr(f"agent.mode.{mode.value}")
        log_event(
            "agent.entry",
            session_id=context.session_id or None,
            mode=mode.value,
            trigger=context.trigger_reason or None,
        )

        if mode is AgentExecutionMode.LEGACY:
            return await self._run_legacy(context, outcome)

        if mode is AgentExecutionMode.SHADOW:
            return await self._run_shadow(context, outcome)

        if mode is AgentExecutionMode.CANARY:
            # mode_for_group 已把 CANARY 解析成 RUNTIME 或 LEGACY，
            # 理论上不会走到这里；保守起见按 legacy 处理。
            return await self._run_legacy(context, outcome)

        # RUNTIME
        return await self._run_runtime(context, outcome)

    # -- 各模式实现 --

    async def _run_legacy(
        self, context: AgentContext, outcome: AgentOutcome
    ) -> AgentOutcome:
        if self._legacy_handler is None:
            outcome.error = "legacy_handler_unavailable"
            return outcome
        try:
            result = await self._legacy_handler(context)
        except Exception as exc:  # noqa: BLE001
            # legacy 编排器在 Phase 6.6 的 state 重构中没做完（详见
            # agent/legacy_orchestrator.py）。这里**降级到 runtime**，而不是
            # 把异常抛给上层：用户该看到的是回复，不是 traceback。
            # 把 mode 设成 legacy 的人想要的正是「保险的回退路径」，
            # 那这条降级恰恰是它该有的行为。
            log_event(
                "agent.legacy_fallback_runtime",
                level="warning",
                session_id=context.session_id or None,
                error=f"{type(exc).__name__}: {exc}",
            )
            return await self._run_runtime(context, outcome)
        outcome.handled = True
        outcome.legacy_result = result
        outcome.used_runtime = False
        return outcome

    async def _run_shadow(
        self, context: AgentContext, outcome: AgentOutcome
    ) -> AgentOutcome:
        """旧路径真正执行；新路径只计算（dry_run，零副作用）。

        Phase 7A：本方法同时是 **Validation 的数据来源** ——
        SHADOW 是唯一能同时拿到两侧行为的模式，因此在这里
        记录行为快照并做比较。
        """
        legacy_started = time.perf_counter()
        legacy_outcome = await self._run_legacy(context, outcome)
        legacy_latency = (time.perf_counter() - legacy_started) * 1000

        shadow_result = None
        runtime_latency = 0.0
        runtime_error = ""
        if self._runtime is not None:
            runtime_started = time.perf_counter()
            try:
                # dry_run=True 由 Runtime 内部传给 ToolExecutor
                shadow_result = await self._runtime.run(context, dry_run=True)
                outcome.journal = getattr(shadow_result, "metadata", {}).get("journal")
                _compare_shadow(context, legacy_outcome.legacy_result, shadow_result)
            except Exception as exc:  # noqa: BLE001 — shadow 失败绝不影响主链路
                runtime_error = f"{type(exc).__name__}: {exc}"
                log_event(
                    "agent.shadow_failed",
                    level="warning",
                    error=runtime_error,
                )
            finally:
                runtime_latency = (time.perf_counter() - runtime_started) * 1000

        # Phase 7A：记录验证数据（失败绝不影响主链路）
        try:
            _record_shadow_validation(
                context,
                legacy_result=legacy_outcome.legacy_result,
                runtime_result=shadow_result,
                legacy_latency_ms=legacy_latency,
                runtime_latency_ms=runtime_latency,
                runtime_error=runtime_error,
            )
        except Exception:  # noqa: BLE001 — 观测失败不影响业务
            pass

        legacy_outcome.mode = AgentExecutionMode.SHADOW.value
        return legacy_outcome

    async def _run_runtime(
        self, context: AgentContext, outcome: AgentOutcome
    ) -> AgentOutcome:
        """新路径正式执行。

        **Phase 5.5 变化（第十六~二十一节）**：RUNTIME 模式**不再**
        per-request fallback 到完整 legacy agent。

        原因：那意味着系统永远维护两套 prompt / tool parsing / recovery /
        memory orchestration，是最大的长期维护成本。

        现在的失败处理：
            基础设施异常 → 记录 error + 返回安全 fallback text
            （文案复用现有错误回复，不新写人格文案）

        **没有部署级回滚了**。`DL_SENPAI_AGENT_MODE=legacy` 这条路已经不通：
        `LegacyOrchestrator.run()` 在 Phase 6.6 的 state 重构里没完成
        （`LegacyRunState` 从未定义，各阶段读的是未定义的局部变量），
        现在它一进来就抛可行动的 RuntimeError。

        所以 legacy 模式下的实际行为是：`AgentEntry` 捕获这个异常并
        **降级到 runtime**（见 `_run_legacy`）。配成 legacy 的人要的是
        「退回旧实现」这层保险，而不是「整个 bot 不再回话」——
        既然旧实现已经不可运行，退到 runtime 才是唯一有意义的结果。
        """
        started_at = time.perf_counter()
        if self._runtime is None:
            # Agent 层不可用：这是部署配置错误，不是请求级故障。
            # 返回安全文案而不是偷偷跑 legacy。
            metrics.incr("agent.runtime_unavailable")
            log_event(
                "agent.runtime_unavailable",
                level="error",
                session_id=context.session_id or None,
            )
            outcome.error = "runtime_unavailable"
            outcome.handled = False
            outcome.stop_reason = StopReason.INFRA_ERROR.value
            return outcome

        journal = ExecutionJournal()
        executor = getattr(self._runtime, "executor", None)
        if executor is not None:
            executor.set_journal(journal)

        try:
            result = await self._runtime.run(context, journal=journal)
        except asyncio.CancelledError:
            # 第五十节：取消必须传播，不能被吞掉后返回兜底文案
            outcome.stop_reason = StopReason.CANCELLED.value
            raise
        except Exception as exc:  # noqa: BLE001 — 基础设施异常
            log_event(
                "agent.runtime_crashed",
                level="error",
                session_id=context.session_id or None,
                error=f"{type(exc).__name__}: {exc}",
                side_effect_committed=journal.side_effect_committed,
            )
            metrics.incr("agent.runtime_crashed")
            outcome.stop_reason = StopReason.INFRA_ERROR.value
            outcome.error = f"{type(exc).__name__}: {exc}"
            outcome.journal = journal
            outcome.handled = False
            # 注意：**不 fallback**。由上层（ResponsePipeline / bridge）
            # 决定是否发一句安全文案。
            return outcome

        outcome.result = result
        outcome.used_runtime = True
        outcome.handled = True
        outcome.journal = journal
        outcome.stop_reason = _stop_reason_value(result)

        # Phase 7B：RUNTIME 模式也要记录自身运行数据。
        #
        # 之前 `_record_*_validation` 只在 SHADOW 路径调用 ——
        # 于是切到 runtime 后采集完全停止（real traffic 永远不涨）。
        #
        # 这里记的是 **Runtime 自己的健康度**：
        #     延迟分阶段 / token / 工具成败 / 记忆命中 / 发送统计
        # 不再做新旧对比（没有 legacy 侧快照），但 report 的
        # Queue / Model / Tool / Memory / Send / Latency 分节仍然有效。
        try:
            _record_runtime_validation(
                context,
                runtime_result=result,
                latency_ms=(time.perf_counter() - started_at) * 1000,
                error="",
            )
        except Exception:  # noqa: BLE001 — 观测失败绝不影响业务
            pass

        return outcome

    async def _fallback(
        self, context: AgentContext, outcome: AgentOutcome, *, reason: str
    ) -> AgentOutcome:
        """切到 legacy。

        **Phase 5.5 后仅 SHADOW / 显式 LEGACY 模式使用**。
        RUNTIME 模式不再调用它（见 `_run_runtime`）。
        """
        if self._legacy_handler is None:
            outcome.error = "no_legacy_handler"
            return outcome

        metrics.incr("agent.fallback")
        log_event(
            "agent.fallback",
            level="warning",
            session_id=context.session_id or None,
            reason=reason,
        )
        legacy_result = await self._legacy_handler(context)
        outcome.legacy_result = legacy_result
        outcome.fallback_used = True
        outcome.used_runtime = False
        outcome.handled = True
        return outcome

    # -- 安全兜底文案（第十八节）--

    @staticmethod
    def safe_error_text(context: AgentContext) -> str:
        """Runtime 失败时给用户的安全文案。

        第十八节明确要求：**复用现有错误回复逻辑，不要新写人格文案**。
        因此这里直接取 `llm._friendly_api_error` 的通用兜底分支，
        保证与现有降级话术一致。
        """
        try:
            from ..llm import _friendly_api_error

            return _friendly_api_error(Exception("infra"))
        except Exception:  # noqa: BLE001 — 兜底失败时给最保守文本
            return "稍等一会儿哦～"

    def need_safe_fallback(self, outcome: AgentOutcome) -> bool:
        """这次结果是否需要用安全文案兜底。"""
        return not outcome.handled and not outcome.fallback_used


def _stop_reason_value(result: Any) -> str:
    reason = getattr(result, "stop_reason", None)
    if reason is None:
        return StopReason.COMPLETED.value
    return getattr(reason, "value", str(reason))


# ================================================================
# Phase 7A：Validation 数据采集
# ================================================================


def _record_shadow_validation(
    context: AgentContext,
    *,
    legacy_result: Any,
    runtime_result: Any,
    legacy_latency_ms: float,
    runtime_latency_ms: float,
    runtime_error: str,
) -> None:
    """把 SHADOW 两侧的行为快照写入 Validation（第六节）。

    **只记行为，不记文本**（第十一节）：
        工具集合、参数键形状、权限拒绝、停止原因、模型调用数、token。
        最终回复文字一律不落盘。
    """
    from ..validation import get_recorder
    from ..validation.models import BehaviorSnapshot

    recorder = get_recorder()
    # 未启用时不构造（省掉 JSON 序列化开销）
    if not recorder.store.enabled:
        return

    legacy_snap = _snapshot_from_legacy(legacy_result, legacy_latency_ms)
    runtime_snap = _snapshot_from_runtime(
        runtime_result, runtime_latency_ms, runtime_error
    )

    # Phase 7A.1：把本轮各层真实插桩的数据一起落盘（第四十七节）。
    # 这是唯一写入点 —— Store 只由 Recorder 访问（第四十八节）。
    from ..validation.telemetry import current_telemetry

    telemetry = current_telemetry()

    # 若 telemetry 里有真实的工具/模型计数，用它覆盖快照里的粗估值，
    # 保证 comparison 与 metrics 口径一致
    if telemetry is not None:
        runtime_snap.tool_calls = telemetry.tool_calls or runtime_snap.tool_calls
        runtime_snap.model_call_count = telemetry.model_call_count
        runtime_snap.total_tokens = telemetry.total_tokens

    extra: dict[str, Any] = getattr(context, "extra", None) or {}
    recorder.record_request(
        trace_id=context.trace_id or "",
        session_id=context.session_id or "",
        user_id=context.user_id or "",
        group_id=context.group_id or "",
        execution_mode="shadow",
        trigger_reason=context.trigger_reason or "",
        # 用户消息只传结构标志，文本本身到这里就丢了
        user_text=context.plain_text or "",
        message_type="private" if context.private_chat else "group",
        has_image=bool(extra.get("has_image")),
        has_reply=bool(extra.get("has_reply")),
        is_to_me=bool(extra.get("is_to_me")),
        command_match=bool(extra.get("command_match")),
        system_prompt=str(extra.get("system_prompt") or ""),
        prompt_messages=list(extra.get("prompt_messages") or []),
        tool_count=int(extra.get("tool_count") or 0),
        model=str(extra.get("model") or ""),
        legacy=legacy_snap,
        runtime=runtime_snap,
        latency=_merge_latency(
            telemetry,
            fallback={
                "total": round(legacy_latency_ms + runtime_latency_ms, 2),
                "legacy_total": round(legacy_latency_ms, 2),
                "runtime_total": round(runtime_latency_ms, 2),
                "queue_wait": float(extra.get("queue_wait_ms") or 0.0),
                "model": float(extra.get("model_ms") or 0.0),
                "tool": float(extra.get("tool_ms") or 0.0),
            },
        ),
        memory=telemetry.memory.as_dict() if telemetry is not None else None,
        send=telemetry.send.as_dict() if telemetry is not None else None,
        # Phase 7A.1：明细一起落盘，便于报告定位到具体工具/模型调用
        tools=[t.as_dict() for t in telemetry.tools] if telemetry is not None else None,
        model_calls=(
            [c.as_dict() for c in telemetry.model_calls]
            if telemetry is not None else None
        ),
        queue=(
            {
                "queue_wait": round(telemetry.queue_wait_ms, 2),
                "queue_depth": telemetry.queue_depth,
                "active_sessions": telemetry.active_sessions,
                "rejected": telemetry.rejected,
            }
            if telemetry is not None else None
        ),
    )


def _record_runtime_validation(
    context: AgentContext,
    *,
    runtime_result: Any,
    latency_ms: float,
    error: str,
) -> None:
    """记录一次 RUNTIME 请求的自身运行数据（Phase 7B）。

    与 `_record_shadow_validation` 的区别：
        SHADOW 记两侧快照 + 对比；
        RUNTIME 只记**自己这一侧** —— 没有 legacy 快照，
        因此 `mismatches` 为空，但延迟 / token / 工具 / 记忆 /
        发送等字段照常有值，报告的性能与成本分节仍然可用。
    """
    from ..validation import get_recorder

    recorder = get_recorder()
    if not recorder.store.enabled:
        return

    from ..validation.telemetry import current_telemetry

    telemetry = current_telemetry()
    runtime_snap = _snapshot_from_runtime(runtime_result, latency_ms, error)
    if telemetry is not None:
        runtime_snap.tool_calls = telemetry.tool_calls or runtime_snap.tool_calls
        runtime_snap.model_call_count = telemetry.model_call_count
        runtime_snap.total_tokens = telemetry.total_tokens

    extra: dict[str, Any] = getattr(context, "extra", None) or {}
    recorder.record_request(
        trace_id=context.trace_id or "",
        session_id=context.session_id or "",
        user_id=context.user_id or "",
        group_id=context.group_id or "",
        execution_mode="runtime",
        trigger_reason=context.trigger_reason or "",
        user_text=context.plain_text or "",
        message_type="private" if context.private_chat else "group",
        has_image=bool(extra.get("has_image")),
        has_reply=bool(extra.get("has_reply")),
        is_to_me=bool(extra.get("is_to_me")),
        command_match=bool(extra.get("command_match")),
        system_prompt=str(extra.get("system_prompt") or ""),
        prompt_messages=list(extra.get("prompt_messages") or []),
        tool_count=int(extra.get("tool_count") or 0),
        model=str(extra.get("model") or ""),
        # 只传 runtime 侧：legacy=None 表示不做对比
        legacy=None,
        runtime=runtime_snap,
        # **关键**：RUNTIME 不做对比 —— 否则 comparator 会报
        # "legacy 侧未产生快照" 的 MEDIUM mismatch（纯噪音）。
        compare_enabled=False,
        latency=_merge_latency(telemetry, fallback={"total": round(latency_ms, 2)}),
        memory=telemetry.memory.as_dict() if telemetry is not None else None,
        send=telemetry.send.as_dict() if telemetry is not None else None,
        tools=[t.as_dict() for t in telemetry.tools] if telemetry is not None else None,
        model_calls=(
            [c.as_dict() for c in telemetry.model_calls]
            if telemetry is not None else None
        ),
        queue=(
            {
                "queue_wait": round(telemetry.queue_wait_ms, 2),
                "queue_depth": telemetry.queue_depth,
                "active_sessions": telemetry.active_sessions,
                "rejected": telemetry.rejected,
            }
            if telemetry is not None else None
        ),
    )


def _merge_latency(telemetry: Any, *, fallback: dict[str, float]) -> dict[str, float]:
    """合并分阶段延迟（第四十三节）。

    优先用真实插桩的阶段值；缺失的键用 fallback 兜底。
    **不做求和校验** —— 阶段可能重叠（第四十六节）。
    """
    out = dict(fallback)
    if telemetry is None:
        return out
    for key, value in telemetry.latency_stages.items():
        if value:
            out[key] = value
    out["total"] = telemetry.total_latency_ms(include_send=False)
    out["total_including_send"] = telemetry.total_latency_ms(include_send=True)
    return out


def _snapshot_from_legacy(result: Any, latency_ms: float) -> Any:
    """从 legacy 的 ChatResult 提取行为快照。

    legacy 结果是 8 个分散字段（card_actions / draw_force / ...），
    这里把它们归一化成"哪些工具被调用"的列表。
    """
    from ..validation.models import BehaviorSnapshot

    if result is None:
        return BehaviorSnapshot(latency_ms=latency_ms, ok=False,
                                error_class="legacy_no_result")

    calls: list[str] = []
    shapes: dict[str, list[str]] = {}

    def mark(name: str, keys: list[str]) -> None:
        calls.append(name)
        shapes[name] = keys

    for action in getattr(result, "card_actions", None) or []:
        mark("card", ["user_id", "card", "kind"])
    for _ in getattr(result, "mute_actions", None) or []:
        mark("mute", ["user_id", "duration", "kind"])
    for _ in getattr(result, "impression_updates", None) or []:
        mark("impression", ["add", "remove", "notes"])
    if getattr(result, "sticker_force", False):
        mark("sticker", ["query"])
    if getattr(result, "voice_force", False):
        mark("voice", ["override"])
    if getattr(result, "draw_force", False):
        mark("draw", ["prompt", "force"])
    if getattr(result, "browse_force", False) or getattr(result, "browse_tasks", None):
        mark("browse", ["tasks"])
    if getattr(result, "searched", False):
        mark("search", ["queries"])

    return BehaviorSnapshot(
        tool_calls=calls,
        tool_argument_shapes=shapes,
        stop_reason="completed",
        latency_ms=latency_ms,
        ok=True,
    )


def _snapshot_from_runtime(result: Any, latency_ms: float, error: str) -> Any:
    """从 AgentResult 提取行为快照。

    Phase 7A.1 语义澄清（重要）：
        `tool_calls` 记录的是**模型请求了哪些工具** —— 这是与 Legacy
        比较"工具选择"维度的依据。SHADOW 下这些调用会以 dry_run 执行，
        因此**模型请求 ≠ 真实副作用**。

        真实执行与否由 `telemetry.tools` 的 outcome 表达
        （dry_run / success / permission_denied / ...）。
        两者分开存，避免把"想禁言"误读成"已禁言"。
    """
    from ..validation.models import BehaviorSnapshot

    if result is None:
        return BehaviorSnapshot(
            latency_ms=latency_ms,
            ok=False,
            error_class="infra_error" if error else "runtime_no_result",
        )

    calls: list[str] = []
    shapes: dict[str, list[str]] = {}
    for call in getattr(result, "tool_calls", None) or []:
        name = getattr(call, "tool_name", "")
        if not name:
            continue
        calls.append(name)
        args = getattr(call, "arguments", None) or {}
        shapes[name] = sorted(args.keys())

    metadata = getattr(result, "metadata", None) or {}
    return BehaviorSnapshot(
        tool_calls=calls,
        tool_argument_shapes=shapes,
        stop_reason=_stop_reason_value(result),
        model_call_count=int(metadata.get("model_calls") or 0),
        prompt_tokens=int(metadata.get("prompt_tokens") or 0),
        completion_tokens=int(metadata.get("completion_tokens") or 0),
        total_tokens=int(metadata.get("total_tokens") or 0),
        latency_ms=latency_ms,
        ok=not error,
        error_class="infra_error" if error else "",
    )


def _compare_shadow(context: AgentContext, legacy_result: Any, shadow_result: Any) -> None:
    """分类比对 shadow 结果（第五十六/五十七节）。

    刻意**不比对最终 LLM 文字**——模型非确定性，比对它只会产生噪声。
    比对的是行为决策：工具集合、continuation。
    """
    from .shadow import classify_mismatch

    legacy_tools = _legacy_tools(legacy_result)
    new_tools = [c.tool_name for c in getattr(shadow_result, "tool_calls", [])]

    mismatch = classify_mismatch(legacy_tools=legacy_tools, new_tools=new_tools)
    metrics.incr("agent.shadow_compared")
    if mismatch:
        metrics.incr("agent.shadow_mismatch")
        metrics.incr(f"agent.shadow_mismatch.{mismatch}")
        log_event(
            "agent.shadow_mismatch",
            level="warning",
            session_id=context.session_id or None,
            category=mismatch,
            legacy_tools=",".join(legacy_tools) or None,
            new_tools=",".join(new_tools) or None,
        )


def _legacy_tools(legacy_result: Any) -> list[str]:
    if legacy_result is None:
        return []
    from .shadow import _legacy_tools_of

    return _legacy_tools_of(legacy_result)


__all__ = ["AgentEntry", "AgentOutcome"]
