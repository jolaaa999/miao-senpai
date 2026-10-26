"""LegacyChatResultAdapter：把共享 ToolCall 结果转成旧 orchestration 能消费的形状。

Phase 6A 第二节。**这个 adapter 绝不重新解析 marker** ——
它只做形状转换：

    MarkerToolAdapter.parse_response(text)
        ↓
    list[ToolCall]
        ↓
    LegacyChatResultAdapter
        ↓
    ChatResult（旧 SenpaiLLM.chat 的返回类型）

为什么要这层：`handlers._handle_chat_inner`（旧编排，已于 2026-10 删除）
依赖 `ChatResult` 的 8 个字段（card_actions / mute_actions / draw_force ...）。
直接改成 list[ToolCall] 会牵动大量下游代码。

保持 adapter 薄的好处：等 Phase 7 彻底删除 legacy 时，只需删这一个文件。

关于 `_finalize_result` 的职责分析（第四节要求先分析再迁移）：

    | 职责 | 是否迁移到 adapter |
    |---|---|
    | marker parsing | ✅ 委托给 MarkerToolAdapter（不再自己解析） |
    | 未启用工具时剥掉 marker | ✅ 保留（防泄漏，属于文本清理） |
    | truncate_reply | ✅ 保留（纯文本处理） |
    | polish_reply | ✅ 保留（复用 style.py，不改语义） |
    | ChatResult 包装 | ✅ 保留（这是 adapter 存在的理由） |
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .tools.models import ToolCall


@dataclass
class LegacyChatResult:
    """旧 orchestration 消费的结果形状。

    字段名与 `llm.ChatResult` **保持一致**，这样 `handlers` 里
    `result.card_actions` 这类访问无需修改。

    注意：这个类只承载"工具已解析出什么"，不承载 polished 文本——
    文本处理留在调用方（那里有 config 上下文）。
    """

    text: str = ""
    card_actions: list[Any] = field(default_factory=list)
    mute_actions: list[Any] = field(default_factory=list)
    sticker_query: str = ""
    sticker_force: bool = False
    voice_force: bool = False
    voice_override: str = ""
    draw_prompt: str = ""
    draw_force: bool = False
    browse_tasks: list[Any] = field(default_factory=list)
    browse_force: bool = False
    searched: bool = False
    impression_updates: list[Any] = field(default_factory=list)


# 工具名 → ChatResult 字段的映射。
#
# 这是"形状转换"的核心：共享层产出统一的 ToolCall，
# legacy 层需要 8 个分散字段。映射表把两者解耦。
_FIELD_NAMES: dict[str, str] = {
    "card": "card_actions",
    "mute": "mute_actions",
    "impression": "impression_updates",
    "sticker": "sticker_query",
    "voice": "voice_override",
    "draw": "draw_prompt",
    "browse": "browse_tasks",
    "search": "searched",
}


class LegacyChatResultAdapter:
    """ToolCall 列表 → LegacyChatResult。

    Args:
        parse_adapter: `MarkerToolAdapter` 实例（共享解析层）。
    """

    def __init__(self, parse_adapter: Any = None) -> None:
        self._adapter = parse_adapter

    def _get_adapter(self, registry: Any = None) -> Any:
        if self._adapter is not None:
            return self._adapter
        from .tools.marker_adapter import MarkerToolAdapter
        from .tools.registry import get_tool_registry

        self._adapter = MarkerToolAdapter(registry or get_tool_registry())
        return self._adapter

    def convert(
        self,
        tool_calls: list[ToolCall],
        *,
        text: str = "",
        searched: bool = False,
    ) -> LegacyChatResult:
        """把 ToolCall 列表转成 LegacyChatResult。

        Args:
            tool_calls: 共享解析层产出的调用列表。
            text: 已剥掉 marker 的正文。
            searched: 是否发生过检索（含多轮 search 循环）。
        """
        result = LegacyChatResult(text=text, searched=searched)

        for call in tool_calls:
            name = call.tool_name
            args = call.arguments or {}

            if name == "card":
                result.card_actions.append(_CardAction.from_args(args))
            elif name == "mute":
                result.mute_actions.append(_MuteAction.from_args(args))
            elif name == "impression":
                result.impression_updates.append(_ImpressionUpdate.from_args(args))
            elif name == "sticker":
                result.sticker_query = str(args.get("query") or "")
                result.sticker_force = bool(call.raw) or bool(result.sticker_query)
            elif name == "voice":
                result.voice_override = str(args.get("override") or "")
                result.voice_force = True
            elif name == "draw":
                result.draw_prompt = str(args.get("prompt") or "")
                result.draw_force = bool(args.get("force", True))
            elif name == "browse":
                result.browse_tasks = list(args.get("tasks") or [])
                result.browse_force = True
            elif name == "search":
                result.searched = True

        return result


# -- 轻量值对象 --
#
# 为什么不用 group_card.CardAction 等原始类：
# 那些类在 executor 层已经被构造过一次。adapter 只需要"形状"，
# 用轻量对象避免把业务模块的构造约束（如必填字段校验）引入转换路径。


@dataclass
class _CardAction:
    user_id: Any = None
    card: str = ""
    kind: str = "request"

    @classmethod
    def from_args(cls, args: dict[str, Any]) -> "_CardAction":
        return cls(
            user_id=args.get("user_id"),
            card=str(args.get("card") or ""),
            kind=str(args.get("kind") or "request"),
        )


@dataclass
class _MuteAction:
    user_id: Any = None
    duration: int = 60
    kind: str = "request"

    @classmethod
    def from_args(cls, args: dict[str, Any]) -> "_MuteAction":
        try:
            duration = int(args.get("duration") or 60)
        except (TypeError, ValueError):
            duration = 60
        return cls(
            user_id=args.get("user_id"),
            duration=duration,
            kind=str(args.get("kind") or "request"),
        )


@dataclass
class _ImpressionUpdate:
    add: list[str] = field(default_factory=list)
    remove: list[str] = field(default_factory=list)
    notes: str | None = None

    @classmethod
    def from_args(cls, args: dict[str, Any]) -> "_ImpressionUpdate":
        return cls(
            add=list(args.get("add") or []),
            remove=list(args.get("remove") or []),
            notes=args.get("notes"),
        )


__all__ = ["LegacyChatResultAdapter", "LegacyChatResult"]
