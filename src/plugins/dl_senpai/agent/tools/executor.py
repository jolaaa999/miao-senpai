"""ToolExecutor：统一的工具执行链。

执行链（第二十二节）：

    ToolCall
      ↓ Registry lookup
      ↓ enabled?
      ↓ permission check   ← 第二次权限检查（第十六节）
      ↓ rate limit
      ↓ timeout
      ↓ execute
      ↓ ToolResult
      ↓ observability

**硬性约束（第二十三节）：Executor 内部禁止出现**
    if tool_name == "draw": ...
    elif tool_name == "mute": ...

一旦出现，Tool Registry 就只是摆设。所有工具差异必须由
`ToolDefinition.executor` 携带。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from ...validation.telemetry import ToolOutcome
from ...observability.logging import log_event
from ...observability.metrics import metrics
from ..journal import SideEffectStatus
from .models import ToolCall, ToolResult
from .registry import ToolDefinition, ToolRegistry


def _instrument_tool(
    call: ToolCall,
    definition: ToolDefinition,
    *,
    outcome: Any,
    duration_ms: float = 0.0,
    permission_ms: float = 0.0,
    permission_denied: bool = False,
    dry_run: bool = False,
    error_type: str = "",
) -> None:
    """把工具终态写进当前请求的 telemetry（Phase 7A.1 第十三条）。

    **唯一采集点** —— 各 executor（execute_draw / execute_search / ...）
    **不得**自己再记一次，否则统计翻倍（第十八节）。

    失败静默：这是观测路径，任何异常都不能影响工具执行结果。
    """
    try:
        from ...validation.telemetry import ToolTelemetry, record_tool_call

        record_tool_call(
            ToolTelemetry(
                tool=call.tool_name,
                call_id=call.id,
                outcome=getattr(outcome, "value", str(outcome)),
                duration_ms=duration_ms,
                permission_ms=permission_ms,
                dry_run=dry_run,
                permission_denied=permission_denied,
                # 第十七节：危险工具单独标记，但**不记 target QQ / 群号 / 名片文本**
                visibility_denied=False,
                error_type=error_type,
            )
        )
    except Exception:  # noqa: BLE001 — 观测失败不影响业务
        pass


@dataclass
class ExecutionOutcome:
    """一次执行的完整结果（含被拒绝的情况）。"""

    result: ToolResult
    blocked: bool = False
    block_reason: str = ""


class ToolExecutor:
    """按定义执行工具调用。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        permissions: Any = None,
        rate_limiter: Any = None,
        journal: Any = None,
    ) -> None:
        self._registry = registry
        self._permissions = permissions
        self._rate_limiter = rate_limiter
        # Phase 5：副作用台账。为 None 时退化为无台账模式（测试/内部调用）
        self._journal = journal
        # tool_name → 上次执行时间（monotonic）
        self._last_run: dict[str, float] = {}

    def set_journal(self, journal: Any) -> None:
        """绑定 ExecutionJournal（每个 Agent 轮次一个）。"""
        self._journal = journal

    # -- 主链路 --

    async def execute(
        self,
        call: ToolCall,
        context: Any = None,
        *,
        dry_run: bool = False,
    ) -> ToolResult:
        """执行一次工具调用。**永不抛异常**——失败也返回 ToolResult。"""
        started = time.perf_counter()
        definition = self._registry.get(call.tool_name)

        # 1) 工具不存在
        if definition is None:
            return self._fail(
                call, "unknown_tool", f"未注册的工具: {call.tool_name}", started
            )

        # 2) 未启用
        if not definition.enabled:
            return self._fail(call, "disabled", f"工具已停用: {call.tool_name}", started)

        # 3) **幂等去重**（第十五节）：同一 tool_call_id 永不执行两次
        if not dry_run and self._journal is not None:
            if self._journal.already_executed(call.id):
                log_event(
                    "tool.duplicate_skipped",
                    level="warning",
                    **call.as_log_fields(),
                    reason="already_executed",
                )
                metrics.incr("tool.duplicate_skipped")
                _instrument_tool(
                    call, definition,
                    outcome=ToolOutcome.SKIPPED,
                    error_type="duplicate_call",
                )
                return ToolResult(
                    tool_name=call.tool_name,
                    tool_call_id=call.id,
                    success=False,
                    error="重复调用已跳过",
                    error_type="duplicate_call",
                    duration_ms=(time.perf_counter() - started) * 1000,
                )

        # 4) 权限（第二次检查：即使 prompt 里没给它，也要再验一次）
        permission_started = time.perf_counter()
        allowed, reason = self._check_permission(definition, call, context)
        permission_ms = (time.perf_counter() - permission_started) * 1000
        if not allowed:
            log_event(
                "tool.permission_denied",
                level="warning",
                **call.as_log_fields(),
                permission=definition.permission or None,
                reason=reason,
            )
            metrics.incr("tool.permission_denied")
            if self._journal is not None:
                entry = self._journal.start(
                    call.id, call.tool_name, side_effect=False,
                    detail={"denied": reason},
                )
                self._journal.complete(call.id, status=SideEffectStatus.SKIPPED)
            _instrument_tool(
                call, definition,
                outcome=ToolOutcome.PERMISSION_DENIED,
                permission_ms=permission_ms,
                permission_denied=True,
                error_type="permission_denied",
            )
            return self._fail(call, "permission_denied", reason, started)

        # 5) 限流
        if not self._check_rate_limit(definition):
            _instrument_tool(
                call, definition,
                outcome=ToolOutcome.FAILURE,
                permission_ms=permission_ms,
                error_type="rate_limited",
            )
            return self._fail(call, "rate_limited", "调用过于频繁", started)

        # 6) dry_run：只报告"本应执行"
        if dry_run:
            if self._journal is not None:
                self._journal.start(
                    call.id, call.tool_name, side_effect=definition.has_side_effect()
                )
                self._journal.complete(call.id, status=SideEffectStatus.DRY_RUN)
            # 第十六节：dry_run 记独立状态，**不混入真实 success**
            _instrument_tool(
                call, definition,
                outcome=ToolOutcome.DRY_RUN,
                permission_ms=permission_ms,
                dry_run=True,
            )
            return ToolResult(
                tool_name=call.tool_name,
                tool_call_id=call.id,
                success=True,
                output="",
                would_execute=True,
                duration_ms=(time.perf_counter() - started) * 1000,
                metadata={"dry_run": True},
            )

        # 7) 执行（带 timeout）
        if definition.executor is None:
            _instrument_tool(
                call, definition,
                outcome=ToolOutcome.FAILURE,
                permission_ms=permission_ms,
                error_type="no_executor",
            )
            return self._fail(call, "no_executor", "工具没有执行器", started)

        # 执行前先记账：**一旦开始执行，就视为副作用可能已发生**。
        # 这是 fallback 安全的关键——宁可保守地禁止 fallback，
        # 也不能在"可能已禁言"的情况下再跑一遍 legacy。
        if self._journal is not None:
            self._journal.start(
                call.id, call.tool_name, side_effect=definition.has_side_effect()
            )

        # 限流占坑：必须在第一个 await 之前完成，否则并发的兄弟调用
        # 会在自己检查时看不到这次调用（详见 _reserve_rate_limit）。
        self._reserve_rate_limit(definition)

        try:
            result = await self._invoke(definition, call, context)
        except asyncio.TimeoutError:
            if self._journal is not None:
                self._journal.complete(
                    call.id, status=SideEffectStatus.FAILED, detail={"timeout": True}
                )
            _instrument_tool(
                call, definition,
                outcome=ToolOutcome.TIMEOUT,
                permission_ms=permission_ms,
                error_type="timeout",
            )
            return self._fail(
                call, "timeout", f"工具超时({definition.timeout}s)", started
            )
        except asyncio.CancelledError:
            # 第四十九/五十节：取消必须传播，不能被当成普通异常吞掉
            if self._journal is not None:
                self._journal.complete(
                    call.id, status=SideEffectStatus.FAILED, detail={"cancelled": True}
                )
            _instrument_tool(
                call, definition,
                outcome=ToolOutcome.CANCELLED,
                permission_ms=permission_ms,
                error_type="cancelled",
            )
            raise
        except Exception as exc:  # noqa: BLE001 — 工具异常必须被隔离
            if self._journal is not None:
                self._journal.complete(
                    call.id, status=SideEffectStatus.FAILED,
                    detail={"error": type(exc).__name__},
                )
            log_event(
                "tool.execute_failed",
                level="warning",
                **call.as_log_fields(),
                error=f"{type(exc).__name__}: {exc}",
            )
            _instrument_tool(
                call, definition,
                outcome=ToolOutcome.FAILURE,
                permission_ms=permission_ms,
                error_type=type(exc).__name__,
            )
            return self._fail(
                call, type(exc).__name__, str(exc)[:200], started
            )

        # 8) 归一化结果
        normalized = self._normalize(result, call, started)
        # 限流时间戳已在 _reserve_rate_limit 里写入（按"开始"计时）。
        # 这里不再覆盖：否则窗口会变成"从完成算起"，慢工具的间隔被拉长一倍。

        if self._journal is not None:
            self._journal.complete(
                call.id,
                status=(
                    SideEffectStatus.COMPLETED
                    if normalized.success
                    else SideEffectStatus.FAILED
                ),
                detail={"success": normalized.success},
            )

        # 9) 观测
        metrics.incr("tool.calls")
        metrics.observe("tool.duration_ms", normalized.duration_ms)
        if not normalized.success:
            metrics.incr("tool.failures")
        # 两个字段集合都含 tool_name/tool_call_id，必须合并而非双重展开
        log_fields = {**call.as_log_fields(), **normalized.as_log_fields()}
        log_event("tool.executed", **log_fields)

        # Phase 7A.1：唯一 terminal metrics（第十三条）。
        # 各 executor 内部**不得**再记一次，否则统计翻倍（第十八节）。
        _instrument_tool(
            call,
            definition,
            outcome=(
                ToolOutcome.SUCCESS if normalized.success else ToolOutcome.FAILURE
            ),
            duration_ms=normalized.duration_ms,
            permission_ms=permission_ms,
            error_type=normalized.error_type or "",
        )
        return normalized

    # -- 内部 --

    async def _invoke(
        self, definition: ToolDefinition, call: ToolCall, context: Any
    ) -> Any:
        """调用执行器，自动处理同步/异步与超时。"""
        import inspect

        value = definition.executor(context, call)  # type: ignore[misc]
        if inspect.isawaitable(value):
            if definition.timeout and definition.timeout > 0:
                return await asyncio.wait_for(value, timeout=definition.timeout)
            return await value
        return value

    def _normalize(
        self, raw: Any, call: ToolCall, started: float
    ) -> ToolResult:
        """把执行器返回值归一化为 ToolResult。

        允许执行器直接返回 ToolResult（推荐），或返回 legacy 形状
        （bool / str / tuple / dict）由这里转换——让底层业务不必改动。
        """
        duration = (time.perf_counter() - started) * 1000
        if isinstance(raw, ToolResult):
            raw.tool_name = raw.tool_name or call.tool_name
            raw.tool_call_id = raw.tool_call_id or call.id
            raw.duration_ms = duration
            return raw

        if raw is None:
            return ToolResult(
                tool_name=call.tool_name, tool_call_id=call.id,
                success=True, duration_ms=duration,
            )
        if isinstance(raw, bool):
            return ToolResult(
                tool_name=call.tool_name, tool_call_id=call.id,
                success=raw, duration_ms=duration,
                error="" if raw else "执行失败",
            )
        if isinstance(raw, str):
            return ToolResult(
                tool_name=call.tool_name, tool_call_id=call.id,
                success=True, output=raw, duration_ms=duration,
            )
        if isinstance(raw, dict):
            success = bool(raw.get("success", True))
            return ToolResult(
                tool_name=call.tool_name, tool_call_id=call.id,
                success=success,
                output=str(raw.get("output", "") or ""),
                data=raw,
                error=str(raw.get("error", "") or ""),
                duration_ms=duration,
            )
        # 兜底：未知形状按成功处理，把 repr 放进 data
        return ToolResult(
            tool_name=call.tool_name, tool_call_id=call.id,
            success=True, data={"value": raw}, duration_ms=duration,
        )

    def _check_permission(
        self, definition: ToolDefinition, call: ToolCall, context: Any
    ) -> tuple[bool, str]:
        """权限检查（Phase 5：真正强制执行第二层授权）。

        委托给 PermissionService；未配置时退化为"只做登记不拦截"
        （与重构前的行为一致——权限由工具自己的业务逻辑承担）。
        """
        if self._permissions is None:
            return True, ""
        try:
            decision = self._permissions.authorize(
                tool=definition, call=call, context=context
            )
        except Exception as exc:  # noqa: BLE001 — 权限服务异常时保守拒绝
            return False, f"权限服务异常: {type(exc).__name__}"
        # 兼容返回 bool 的旧实现
        if isinstance(decision, bool):
            return decision, "" if decision else "denied"
        return bool(getattr(decision, "allowed", False)), str(
            getattr(decision, "reason", "") or ""
        )

    def _check_rate_limit(self, definition: ToolDefinition) -> bool:
        if definition.rate_limit <= 0:
            return True
        last = self._last_run.get(definition.name)
        if last is None:
            return True
        return (time.perf_counter() - last) >= definition.rate_limit

    def _reserve_rate_limit(self, definition: ToolDefinition) -> None:
        """占用限流名额（在**真正要执行**的工具上调用）。

        为什么要把"占坑"从 _check_rate_limit 里拆出来：

        1. 并发安全：同一轮回复里的多个工具会并发执行。若沿用"执行完
           再记录"，同批里的两个同工具调用会同时通过检查，限流失效。
           而"检查→占坑"之间没有 await，不会被其他协程插队，所以只要
           把占坑放在第一个 await 之前就够。
        2. 不误伤 dry_run：shadow 模式全程 dry_run，如果检查处就占坑，
           影子推演会白白吃掉真实调用的限流额度。
        """
        if definition.rate_limit > 0:
            self._last_run[definition.name] = time.perf_counter()

    def _fail(
        self, call: ToolCall, error_type: str, error: str, started: float
    ) -> ToolResult:
        duration = (time.perf_counter() - started) * 1000
        metrics.incr("tool.failures")
        log_event(
            "tool.failed",
            level="warning",
            **call.as_log_fields(),
            error_type=error_type,
            error=error[:200],
        )
        return ToolResult(
            tool_name=call.tool_name,
            tool_call_id=call.id,
            success=False,
            error=error,
            error_type=error_type,
            duration_ms=duration,
        )


__all__ = ["ToolExecutor", "ExecutionOutcome"]
