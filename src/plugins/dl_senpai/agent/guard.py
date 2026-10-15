"""AgentGuard：Agent 循环的护栏。

保护四件事（第三十三/三十四/三十五节）：

    max_steps              —— 最多几轮模型调用
    max_tool_calls         —— 一轮 Agent 最多执行几个工具
    same_tool_repeat_limit —— 同一工具+同一参数重复几次就停
    timeout                —— 整体墙钟超时
    token_budget           —— token 预算

设计要点：Guard **不抛异常中断**，而是返回"是否允许继续 + 停止原因"。
这样 AgentRuntime 可以正常收尾（把已有回复发出去），而不是让用户
面对一个静默失败的机器人。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StopReason(str, Enum):
    """Agent 循环结束的原因。"""

    FINISHED = "finished"                 # 模型给出最终回复
    NO_TOOL_CALL = "no_tool_call"         # 模型没有要求调用工具
    MAX_STEPS = "max_steps"               # 达到最大步数
    MAX_TOOL_CALLS = "max_tool_calls"     # 达到最大工具调用数
    REPEAT_LIMIT = "repeat_limit"         # 同一工具重复调用
    TIMEOUT = "timeout"                   # 整体超时
    TOKEN_BUDGET = "token_budget"         # token 预算耗尽
    GUARD_BLOCKED = "guard_blocked"       # 其他护栏拦截
    ERROR = "error"                       # 执行异常


@dataclass
class TokenUsage:
    """token 用量统计。

    provider 不返回 usage 时字段保持 None —— **不估算、不假装是真值**
    （第三十五节明确要求）。
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    calls: int = 0

    def add(self, prompt: int | None, completion: int | None, total: int | None) -> None:
        self.calls += 1
        if prompt is not None:
            self.prompt_tokens = (self.prompt_tokens or 0) + prompt
        if completion is not None:
            self.completion_tokens = (self.completion_tokens or 0) + completion
        if total is not None:
            self.total_tokens = (self.total_tokens or 0) + total
        elif prompt is not None and completion is not None:
            # 只在两个分量都已知时才推导，否则保持 None
            self.total_tokens = (self.total_tokens or 0) + prompt + completion

    @property
    def known(self) -> bool:
        """是否至少拿到过一次真实 usage。"""
        return self.total_tokens is not None or self.prompt_tokens is not None

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "llm_calls": self.calls,
        }


@dataclass
class GuardConfig:
    """护栏阈值。"""

    max_steps: int = 4
    max_tool_calls: int = 8
    same_tool_repeat_limit: int = 2
    timeout: float = 90.0
    token_budget: int | None = None


@dataclass
class AgentGuard:
    """执行护栏。每个 Agent 轮次创建一个实例。"""

    config: GuardConfig = field(default_factory=GuardConfig)
    # 工具指纹 → 已调用次数
    _fingerprints: dict[str, int] = field(default_factory=dict)
    _tool_calls: int = 0
    _steps: int = 0
    _started_at: float = field(default_factory=time.perf_counter)
    _usage: TokenUsage = field(default_factory=TokenUsage)

    # -- 步数 --

    @property
    def steps(self) -> int:
        return self._steps

    @property
    def tool_calls(self) -> int:
        return self._tool_calls

    @property
    def usage(self) -> TokenUsage:
        return self._usage

    def elapsed(self) -> float:
        return time.perf_counter() - self._started_at

    def begin_step(self) -> None:
        self._steps += 1

    def check_step(self) -> StopReason | None:
        if self._steps >= self.config.max_steps:
            return StopReason.MAX_STEPS
        return None

    # -- 工具调用 --

    def check_tool_call(self, fingerprint: str) -> StopReason | None:
        """执行工具**之前**检查是否允许。"""
        if self._tool_calls >= self.config.max_tool_calls:
            return StopReason.MAX_TOOL_CALLS
        count = self._fingerprints.get(fingerprint, 0)
        if count >= self.config.same_tool_repeat_limit:
            return StopReason.REPEAT_LIMIT
        return None

    def record_tool_call(self, fingerprint: str) -> None:
        self._tool_calls += 1
        self._fingerprints[fingerprint] = self._fingerprints.get(fingerprint, 0) + 1

    # -- 超时 / 预算 --

    def check_timeout(self) -> StopReason | None:
        if self.config.timeout > 0 and self.elapsed() >= self.config.timeout:
            return StopReason.TIMEOUT
        return None

    def check_budget(self) -> StopReason | None:
        budget = self.config.token_budget
        if budget is None:
            return None
        total = self._usage.total_tokens
        if total is not None and total >= budget:
            return StopReason.TOKEN_BUDGET
        return None

    def record_usage(self, prompt: int | None, completion: int | None, total: int | None) -> None:
        self._usage.add(prompt, completion, total)

    def check_all(self) -> StopReason | None:
        """综合检查（每次循环开头调用）。"""
        return (
            self.check_timeout()
            or self.check_budget()
            or self.check_step()
        )

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "steps": self._steps,
            "tool_calls": self._tool_calls,
            "elapsed_ms": round(self.elapsed() * 1000, 1),
            **self._usage.as_log_fields(),
        }


__all__ = ["AgentGuard", "GuardConfig", "StopReason", "TokenUsage"]
