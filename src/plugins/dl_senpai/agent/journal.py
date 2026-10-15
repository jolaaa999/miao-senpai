"""ExecutionJournal：副作用台账 + 幂等去重 + fallback 安全闸门。

Phase 5 风险最高的地方（第十三/十四节）：

    Runtime 已经执行了 mute(user_id=456)
            ↓
    ResponsePipeline 崩溃
            ↓
    此时若 fallback 到 legacy
            ↓
    legacy 再禁言一次 → **用户被禁言两次**

Journal 用两条独立机制防止这件事：

1. **幂等去重**（第十五节）：同一 `tool_call_id` 永不执行两次。
   覆盖"重试""恢复"等同一 trace 内的重复调用。

2. **副作用台账**：每个会产生不可逆后果的工具执行后打标，
   `fallback_allowed` 立即变为 False。即使后续发生基础设施故障，
   也不再允许 legacy 重跑。

"不可逆"的判定（`IRREVERSIBLE_TOOLS`）：
    mute / card / impression / voice / sticker / draw 的**发送部分**
    都是对外可见或已落盘的，重跑会造成重复。
    search / browse 是只读的，重跑无副作用。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class SideEffectStatus(str, Enum):
    """副作用条目的状态。"""

    STARTED = "started"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"      # 权限拒绝/护栏拦截，未真正执行
    DRY_RUN = "dry_run"      # shadow 模式，只标记不执行


# 会产生**不可逆**后果的工具。
#
# 判定标准：重跑一次会不会让群友看到不同的结果 / 重复的对外动作。
# mute 禁言两次、card 改两次、发两条语音——都是用户可感知的重复。
IRREVERSIBLE_TOOLS: frozenset[str] = frozenset({
    "mute",
    "card",
    "impression",
    "voice",
    "sticker",
    "draw",
})

# 只读工具：重跑无副作用。
READ_ONLY_TOOLS: frozenset[str] = frozenset({"search", "browse"})


@dataclass
class JournalEntry:
    """一条副作用记录。"""

    tool_call_id: str
    tool: str
    status: SideEffectStatus = SideEffectStatus.STARTED
    # 是否不可逆（决定 fallback 是否还允许）
    side_effect: bool = False
    started_at: float = 0.0
    completed_at: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> float:
        if not self.started_at or not self.completed_at:
            return 0.0
        return (self.completed_at - self.started_at) * 1000

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "tool_call_id": self.tool_call_id,
            "status": self.status.value,
            "side_effect": self.side_effect,
            "duration_ms": round(self.duration_ms, 1),
        }


class ExecutionJournal:
    """一轮 Agent 执行的副作用台账。

    **不持久化**：它是 single-turn runtime state 的一部分，
    与 `AgentState` 同生命周期。
    """

    def __init__(self) -> None:
        self._entries: list[JournalEntry] = []
        self._by_call_id: dict[str, JournalEntry] = {}
        # 已执行过的 tool_call_id（幂等去重）
        self._executed: set[str] = set()

    # -- 幂等 --

    def already_executed(self, tool_call_id: str) -> bool:
        """该 tool_call_id 是否已经执行过（含执行中）。

        这是防重复的第一道闸门：同一 id 永不执行第二次。
        """
        return tool_call_id in self._executed

    def mark_executed(self, tool_call_id: str) -> bool:
        """登记执行。返回 False 表示**已经执行过**（调用方应跳过）。"""
        if tool_call_id in self._executed:
            return False
        self._executed.add(tool_call_id)
        return True

    # -- 记录 --

    def start(
        self,
        tool_call_id: str,
        tool: str,
        *,
        side_effect: bool | None = None,
        detail: dict[str, Any] | None = None,
    ) -> JournalEntry:
        entry = JournalEntry(
            tool_call_id=tool_call_id,
            tool=tool,
            status=SideEffectStatus.STARTED,
            side_effect=is_irreversible(tool) if side_effect is None else side_effect,
            started_at=time.perf_counter(),
            detail=dict(detail or {}),
        )
        self._entries.append(entry)
        self._by_call_id[tool_call_id] = entry
        self._executed.add(tool_call_id)
        return entry

    def complete(
        self, tool_call_id: str, *, status: SideEffectStatus = SideEffectStatus.COMPLETED,
        detail: dict[str, Any] | None = None,
    ) -> JournalEntry | None:
        entry = self._by_call_id.get(tool_call_id)
        if entry is None:
            return None
        entry.status = status
        entry.completed_at = time.perf_counter()
        if detail:
            entry.detail.update(detail)
        return entry

    # -- 安全闸门 --

    @property
    def side_effect_committed(self) -> bool:
        """是否已经发生**不可逆**副作用。

        只要为 True，`fallback_allowed` 就必须为 False。

        **关键：失败也算已发生。** 工具失败可能是"请求已发出但对端未响应"
        （例如禁言请求超时），此时重跑 legacy 有真实的重复风险。
        因此只有 DRY_RUN / SKIPPED（确定没执行）才不算。
        """
        for entry in self._entries:
            if not entry.side_effect:
                continue
            if entry.status in (
                SideEffectStatus.STARTED,
                SideEffectStatus.COMPLETED,
                SideEffectStatus.FAILED,
            ):
                return True
        return False

    @property
    def fallback_allowed(self) -> bool:
        """是否还允许 fallback 到 legacy。

        注意：这只是**副作用维度**的判定。
        完整判定还需结合 StopReason（见 `mode.should_fallback`）。
        """
        return not self.side_effect_committed

    # -- 查询 --

    @property
    def entries(self) -> list[JournalEntry]:
        return list(self._entries)

    def completed_tools(self) -> list[str]:
        return [
            e.tool for e in self._entries
            if e.status is SideEffectStatus.COMPLETED
        ]

    def summary(self) -> dict[str, Any]:
        return {
            "entries": len(self._entries),
            "side_effect_committed": self.side_effect_committed,
            "fallback_allowed": self.fallback_allowed,
            "tools": [e.tool for e in self._entries],
        }


def is_irreversible(tool_name: str) -> bool:
    """该工具是否会产生不可逆副作用。"""
    return tool_name in IRREVERSIBLE_TOOLS


__all__ = [
    "ExecutionJournal",
    "JournalEntry",
    "SideEffectStatus",
    "IRREVERSIBLE_TOOLS",
    "READ_ONLY_TOOLS",
    "is_irreversible",
]
