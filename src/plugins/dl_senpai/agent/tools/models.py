"""统一工具调用模型。

Phase 4 的核心抽象：把"模型想调用一个工具"这件事从**文本协议**里解耦出来。

    CARD/MUTE/DRAW/... 的 marker 文本   ──MarkerToolAdapter──┐
                                                              ├──→ ToolCall
    未来 OpenAI native tool_calls                        ─────┘

这样 AgentRuntime 只认识 `ToolCall`，不认识 `<<<CARD>>>`。
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# ToolCall id 的全局序列号。
#
# 为什么必须全局唯一而不是"每轮从 0 开始"：
# ExecutionJournal 的幂等去重以 tool_call_id 为键。如果两轮 Agent
# 循环里各自产生 `search-0`，第二次就会被误判为"重复调用"而跳过，
# 导致本该继续的循环提前结束。
_call_seq = itertools.count(1)


class ToolCallSource(str, Enum):
    """工具调用的来源。"""

    # 从模型文本里的 marker 解析出来（当前生产路径）
    MARKER = "marker"
    # 来自 OpenAI 原生 tool_calls（扩展点，当前不接管）
    NATIVE = "native"
    # 由系统内部产生（例如恢复重试时的人工注入）
    INTERNAL = "internal"


@dataclass
class ToolCall:
    """一次工具调用请求。

    Attributes:
        id: 调用唯一 id（用于把结果与请求对应起来）。
        tool_name: 工具名（如 `card` / `draw`）。
        arguments: 解析后的参数。不同工具的 schema 不同：
            - card: {"user_id": int, "card": str, "kind": str}
            - draw: {"prompt": str, "force": bool}
            - search: {"queries": list[str]}
        raw: 原始文本片段（marker 原文 / native payload），用于日志与调试。
        source: 来源。
        marker: 命中的 marker 名（如 `CARD`），仅 marker 来源有值。
        position: 在原文中的位置，用于保持多 marker 的解析顺序稳定性。
    """

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    raw: str = ""
    source: ToolCallSource = ToolCallSource.MARKER
    marker: str = ""
    position: int = 0

    def __post_init__(self) -> None:
        if not self.id:
            # 稳定但唯一：工具名 + 位置。
            #
            # 注意：marker 解析每轮都会从 position 0 重新编号，因此
            # 单纯用 "name-position" 会让**不同轮次**的合法调用撞 id，
            # 被幂等去重误伤（实测会导致 REPEAT_LIMIT 测试失效）。
            # 因此这里加一个单调递增的序列号，保证进程内全局唯一。
            self.id = f"{self.tool_name}-{next(_call_seq)}-{self.position}"

    def fingerprint(self) -> str:
        """参数指纹，用于 AgentGuard 的重复调用检测。

        使用排序后的 JSON，保证 {"a":1,"b":2} 与 {"b":2,"a":1} 得到同一指纹
        ——否则模型换个键序就能绕过重复检测。
        """
        import json

        try:
            canonical = json.dumps(self.arguments, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            canonical = repr(sorted(self.arguments.items()))
        return f"{self.tool_name}:{canonical}"

    def arguments_summary(self, max_len: int = 120) -> str:
        """参数摘要（日志用，必须脱敏）。"""
        import json

        try:
            text = json.dumps(self.arguments, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            text = repr(self.arguments)
        text = text.replace("\n", " ")
        if len(text) > max_len:
            text = text[: max_len - 1] + "…"
        return text

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "tool_call_id": self.id,
            "source": self.source.value,
            "arguments_summary": self.arguments_summary(),
        }


class Continuation(str, Enum):
    """执行完一个工具后，是否需要回到模型继续。

    这是 Phase 4 的关键设计（第二十五节）：不同工具行为不同，
    但 AgentRuntime **不应该**用 `if search: call_llm_again` 来表达。
    由工具自己声明，Runtime 只按声明执行。
    """

    # 纯副作用，不回模型：CARD / MUTE / DRAW / VOICE / STICKER / IMPRESSION
    SIDE_EFFECT_ONLY = "side_effect_only"
    # 需要把结果回填给模型，让它继续作答：SEARCH / BROWSE
    RETURN_TO_MODEL = "return_to_model"
    # 不执行任何东西（用于占位/禁用）
    NONE = "none"


@dataclass
class ToolResult:
    """一次工具执行的结果。

    统一形状是关键：重构前各工具返回 bool / str / tuple / dict / None，
    上层只能靠猜。现在统一为 ToolResult。
    """

    tool_name: str
    tool_call_id: str = ""
    success: bool = False
    # 供模型阅读的结果文本（会回填到 messages）
    output: str = ""
    # 结构化输出（供业务代码使用，例如生图路径）
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_type: str = ""
    duration_ms: float = 0.0
    # dry_run 时标记"本应执行"
    would_execute: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "tool_call_id": self.tool_call_id or None,
            "success": self.success,
            "duration_ms": round(self.duration_ms, 1),
            "error_type": self.error_type or None,
            "would_execute": self.would_execute or None,
        }


__all__ = [
    "ToolCall",
    "ToolResult",
    "ToolCallSource",
    "Continuation",
]
