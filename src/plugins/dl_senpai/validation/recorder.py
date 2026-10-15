"""ValidationRecorder：真实请求的行为记录入口。

Phase 7A 第六/七节。

    请求 ──→ AgentEntry / Shadow
              ↓
         ValidationRecorder.record()
              ↓
         ValidationStore (JSONL)
              ↓
         scripts/validation_report.py

**设计原则：记录器必须是"哑"的、安全的、便宜的。**

    - 它不认识业务逻辑，只接结构化快照
    - 所有 id 在写入前脱敏（privacy 层）
    - 任何写入异常都被吞掉 —— 观测绝不能影响回复
      （这与 observability 层的一贯原则一致）

**默认关闭**：未设 `DL_SENPAI_VALIDATION=1` 时不写任何文件，
           也不会在内存里堆积（避免长时间运行内存泄漏）。
"""

from __future__ import annotations

import time
from typing import Any

from .comparator import compare, count_by_severity, count_by_type
from .models import (
    BehaviorSnapshot,
    ErrorClass,
    Mismatch,
    MismatchType,
    Severity,
    ValidationRecord,
)
from .privacy import (
    anonymize,
    message_profile,
    prompt_profile,
)
from .store import ValidationStore


class ValidationRecorder:
    """把一次请求的行为数据记录并落盘。

    Args:
        store: 存储后端。None 时使用默认（读环境变量决定是否启用）。
        expected_differences: 已知合理差异（例如 Legacy 无 Knowledge Tool）。
    """

    def __init__(
        self,
        store: ValidationStore | None = None,
        *,
        expected_differences: set[MismatchType] | None = None,
    ) -> None:
        self.store = store or ValidationStore()
        self.expected_differences = expected_differences or set()
        # 进程内计数（即使未落盘也便于 /health 之类查看）
        self.counters: dict[str, int] = {
            "recorded": 0,
            "critical": 0,
            "high": 0,
            "write_failed": 0,
        }

    # -- 构造记录（脱敏在这里发生）--

    def build_record(
        self,
        *,
        trace_id: str = "",
        session_id: str = "",
        user_id: str = "",
        group_id: str = "",
        execution_mode: str = "",
        trigger_reason: str = "",
        user_text: str = "",
        message_type: str = "group",
        has_image: bool = False,
        has_reply: bool = False,
        is_to_me: bool = False,
        command_match: bool = False,
        system_prompt: str = "",
        prompt_messages: list[dict[str, Any]] | None = None,
        tool_count: int = 0,
        legacy: BehaviorSnapshot | None = None,
        runtime: BehaviorSnapshot | None = None,
        # RUNTIME 模式只记录自身指标，不做对比（Phase 7B）
        compare_enabled: bool = True,
        latency: dict[str, float] | None = None,
        memory: dict[str, Any] | None = None,
        send: dict[str, Any] | None = None,
        model: str = "",
        error_class: str = "",
        error_detail: str = "",
    ) -> ValidationRecord:
        """构造一条**已脱敏**的记录。

        调用方传入的是真实 id 与真实文本，本方法负责：
            - id → 匿名 hash
            - 文本 → 结构画像（不保留原文）
        """
        mismatches: list[Mismatch] = []
        if compare_enabled and (legacy is not None or runtime is not None):
            mismatches = compare(
                legacy, runtime, expected_differences=self.expected_differences
            )

        record = ValidationRecord(
            trace_id=trace_id,
            session_hash=anonymize(session_id, kind="session"),
            user_hash=anonymize(user_id, kind="user"),
            group_hash=anonymize(group_id, kind="group"),
            execution_mode=execution_mode,
            trigger_reason=trigger_reason,
            message=message_profile(
                user_text,
                message_type=message_type,
                has_image=has_image,
                has_reply=has_reply,
                is_to_me=is_to_me,
                command_match=command_match,
            ),
            prompt={
                **prompt_profile(
                    system_prompt=system_prompt,
                    messages=prompt_messages,
                    tool_count=tool_count,
                ),
                # 模型名不是隐私，且对成本分析必要（第二十节）
                "model": model,
            },
            legacy=legacy,
            runtime=runtime,
            mismatches=mismatches,
            latency=dict(latency or {}),
            error_class=error_class,
            error_detail=_safe_detail(error_detail),
            timestamp=time.time(),
        )
        return record

    # -- 落盘 --

    def record(self, record: ValidationRecord, **extra: Any) -> bool:
        """写入记录。**永不抛异常**（观测不能影响回复）。

        `extra` 用于附带旁路指标（memory / send），它们不参与 compare。
        """
        try:
            payload = record.as_dict()
            # memory / send 是旁路指标，不参与 compare，因此在这里合并
            for key, value in (extra or {}).items():
                if value:
                    payload[key] = value
            written = self.store.append(_DictRecord(payload))
            self.counters["recorded"] += 1
            for m in record.mismatches:
                if m.severity is Severity.CRITICAL:
                    self.counters["critical"] += 1
                elif m.severity is Severity.HIGH:
                    self.counters["high"] += 1
            return written
        except Exception:  # noqa: BLE001 — 观测失败绝不影响业务
            self.counters["write_failed"] += 1
            return False

    def record_request(self, **kwargs: Any) -> ValidationRecord | None:
        """便捷入口：构造 + 落盘。

        Phase 7A.1：`memory` / `send` / `tools` / `model_calls` / `cost`
        等都属于**旁路指标**（不参与 compare），因此从 build_record 的
        签名里摘出来，写入时合并进 payload。
        """
        side_keys = ("memory", "send", "tools", "model_calls", "cost", "queue")
        side = {k: kwargs.pop(k, None) for k in side_keys}
        try:
            record = self.build_record(**kwargs)
        except Exception:  # noqa: BLE001
            self.counters["write_failed"] += 1
            return None
        self.record(record, **{k: v for k, v in side.items() if v is not None})
        return record

    # -- 汇总 --

    def summary(self, *, since: float | None = None) -> dict[str, Any]:
        """生成 summary（第十六节）。"""
        from . import metrics as _metrics

        rows = self.store.read(since=since)
        out = _metrics.summarize(rows)
        out["queue"] = _metrics.summarize_queue(rows)
        out["tools"] = _metrics.summarize_tools(rows)
        out["memory"] = _metrics.summarize_memory(rows)
        out["send"] = _metrics.summarize_send(rows)
        out["counters"] = dict(self.counters)
        return out


class _DictRecord:
    """把已构造好的 dict 适配成 store.append 需要的协议。"""

    __slots__ = ("_payload",)

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def as_dict(self) -> dict[str, Any]:
        return self._payload


def _safe_detail(detail: str) -> str:
    """错误详情也要脱敏并截断（可能含 URL / key）。"""
    if not detail:
        return ""
    from .privacy import redact_secrets

    return redact_secrets(str(detail))[:300]


# ================================================================
# 默认期望差异
# ================================================================


def default_expected_differences() -> set[MismatchType]:
    """已知的合理差异集合。

    目前为空 —— 这是**刻意的**：
        在拿到真实数据之前，任何"已知差异"都是猜测。
        与其预先豁免掉可能的问题，不如先全部报出来看。
    """
    return set()


# 全局记录器（生产用单例）
_recorder: ValidationRecorder | None = None


def get_recorder() -> ValidationRecorder:
    global _recorder
    if _recorder is None:
        _recorder = ValidationRecorder(
            expected_differences=default_expected_differences()
        )
    return _recorder


def reset_recorder() -> None:
    """重置（测试用）。"""
    global _recorder
    _recorder = None


__all__ = [
    "ValidationRecorder",
    "get_recorder",
    "reset_recorder",
    "default_expected_differences",
    "BehaviorSnapshot",
    "MismatchType",
    "Severity",
    "ErrorClass",
    "count_by_severity",
    "count_by_type",
]
