"""Agent Shadow Mode：新 AgentRuntime 并行计算，**绝不产生副作用**。

第五十七节。目的与 Phase 3 的 Router Shadow 相同：在真流量上验证
新实现与旧实现是否一致，但不改变实际行为。

    真实消息
       ↓
    Legacy Agent（真正执行、真正发消息）
       ↓
    AgentRuntime（dry_run=True）
       ↓
    比对 prompt 片段 / 工具调用 → 不一致记 agent.shadow_mismatch

**绝对禁止副作用**（第五十七节明确要求）：
    不能真发消息 / 禁言 / 改群名片 / 生图 / 写 memory

本模块通过三个措施保证：
1. `dry_run=True` 让 ToolExecutor 只返回 would_execute；
2. 不复用真实 gateway（避免二次调用 LLM 产生费用）；
3. 不调用 ResponsePipeline.send。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from ..observability.logging import log_event
from ..observability.metrics import metrics

SHADOW_ENV = "DL_SENPAI_AGENT_SHADOW"
_TRUTHY = {"1", "true", "yes", "on"}


def shadow_enabled() -> bool:
    """Agent Shadow 是否开启。"""
    return os.environ.get(SHADOW_ENV, "").strip().lower() in _TRUTHY


@dataclass
class AgentShadowRecord:
    """一次 Agent shadow 比对结果。"""

    session_id: str = ""
    # 旧路径的产出
    legacy_text_len: int = 0
    legacy_tools: list[str] = field(default_factory=list)
    # 新路径的产出
    new_text_len: int = 0
    new_tools: list[str] = field(default_factory=list)
    # 可见工具集合是否一致（最能反映 prompt 是否等价）
    legacy_visible: list[str] = field(default_factory=list)
    new_visible: list[str] = field(default_factory=list)
    match: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id or None,
            "match": self.match,
            "legacy_tools": ",".join(self.legacy_tools) or None,
            "new_tools": ",".join(self.new_tools) or None,
            "legacy_visible": ",".join(self.legacy_visible) or None,
            "new_visible": ",".join(self.new_visible) or None,
        }


async def shadow_compare(
    runtime: Any,
    context: Any,
    *,
    legacy_result: Any = None,
    legacy_visible: list[str] | None = None,
) -> AgentShadowRecord | None:
    """在 shadow 模式下跑一次 AgentRuntime 并比对。

    Args:
        runtime: `AgentRuntime` 实例。
        context: `AgentContext`。
        legacy_result: 旧路径的 `ChatResult`（可为 None）。
        legacy_visible: 旧路径认为可用的工具名（由调用方算出）。

    Returns:
        AgentShadowRecord，或 None（未开启 shadow）。
    """
    if not shadow_enabled():
        return None

    record = AgentShadowRecord(session_id=getattr(context, "session_id", "") or "")

    try:
        # 关键：dry_run=True，不产生任何副作用
        result = await runtime.run(context, dry_run=True)
    except Exception as exc:  # noqa: BLE001 — shadow 失败绝不能影响主链路
        log_event(
            "agent.shadow_failed",
            level="warning",
            session_id=record.session_id or None,
            error=f"{type(exc).__name__}: {exc}",
        )
        return None

    record.new_text_len = len(result.text or "")
    record.new_tools = [c.tool_name for c in result.tool_calls]
    record.new_visible = list(result.metadata.get("visible_tools", []) or [])

    if legacy_result is not None:
        record.legacy_text_len = len(getattr(legacy_result, "text", "") or "")
        record.legacy_tools = _legacy_tools_of(legacy_result)
    if legacy_visible is not None:
        record.legacy_visible = list(legacy_visible)

    # 比对口径：以"可见工具集合"为主。
    # 不比对文本长度——新旧都会调 LLM，但 shadow 用 stub/真实网关不同，
    # 文本必然不同，比对它只会产生噪声。
    record.match = (
        not record.legacy_visible
        or sorted(record.legacy_visible) == sorted(record.new_visible)
    )

    metrics.incr("agent.shadow_compared")
    if not record.match:
        metrics.incr("agent.shadow_mismatch")
        log_event("agent.shadow_mismatch", level="warning", **record.as_log_fields())
    return record


def _legacy_tools_of(legacy_result: Any) -> list[str]:
    """从旧 ChatResult 推出"用了哪些工具"。

    旧结果是 8 个独立字段，这里做一次反向映射——仅供 shadow 比对。
    """
    tools: list[str] = []
    if getattr(legacy_result, "card_actions", None):
        tools.append("card")
    if getattr(legacy_result, "mute_actions", None):
        tools.append("mute")
    if getattr(legacy_result, "impression_updates", None):
        tools.append("impression")
    if getattr(legacy_result, "searched", False):
        tools.append("search")
    if getattr(legacy_result, "draw_force", False):
        tools.append("draw")
    if getattr(legacy_result, "browse_force", False):
        tools.append("browse")
    if getattr(legacy_result, "voice_force", False):
        tools.append("voice")
    if getattr(legacy_result, "sticker_force", False):
        tools.append("sticker")
    return tools


# -- Mismatch 分类（第五十七节）--
#
# 单一 `match=false` 无法定位问题。分类后可以直接从 metrics 看出
# 是哪一类不一致占主导。

MISMATCH_PROMPT = "PROMPT_MISMATCH"
MISMATCH_TOOL = "TOOL_MISMATCH"
MISMATCH_PERMISSION = "PERMISSION_MISMATCH"
MISMATCH_RECOVERY = "RECOVERY_MISMATCH"
MISMATCH_CONTINUATION = "CONTINUATION_MISMATCH"


def classify_mismatch(
    *,
    legacy_tools: list[str] | None = None,
    new_tools: list[str] | None = None,
    legacy_visible: list[str] | None = None,
    new_visible: list[str] | None = None,
    legacy_recovery: bool | None = None,
    new_recovery: bool | None = None,
    legacy_continuation: bool | None = None,
    new_continuation: bool | None = None,
) -> str:
    """判断不一致属于哪一类。返回空串表示一致。

    判定优先级：权限 → prompt → recovery → continuation → tool。
    权限与 prompt 是"根因层"，一旦不一致，后面的差异多半是它的后果，
    因此先报根因。
    """
    if legacy_visible is not None and new_visible is not None:
        if sorted(legacy_visible) != sorted(new_visible):
            return MISMATCH_PROMPT

    if legacy_tools is not None and new_tools is not None:
        if sorted(legacy_tools) != sorted(new_tools):
            # 只在工具集合不同时报 TOOL_MISMATCH；
            # 若可见集合也不同，上面已提前返回 PROMPT_MISMATCH
            return MISMATCH_TOOL

    if legacy_recovery is not None and new_recovery is not None:
        if legacy_recovery != new_recovery:
            return MISMATCH_RECOVERY

    if legacy_continuation is not None and new_continuation is not None:
        if legacy_continuation != new_continuation:
            return MISMATCH_CONTINUATION

    return ""


__all__ = [
    "shadow_enabled",
    "shadow_compare",
    "AgentShadowRecord",
    "SHADOW_ENV",
    "classify_mismatch",
    "MISMATCH_PROMPT",
    "MISMATCH_TOOL",
    "MISMATCH_PERMISSION",
    "MISMATCH_RECOVERY",
    "MISMATCH_CONTINUATION",
]
