"""LegacyToolExecutionContextAdapter：把 legacy 的散装上下文转成 AgentContext。

Phase 6A 第十一节。Legacy 的 `_handle_chat_inner` 手里是一堆散变量
（bot / event / config / session_id / sender / intent_text ...），
而共享的 `ToolExecutor` 需要一个 `AgentContext`。

这个 adapter **只做上下文转换**，不实现任何 Tool 行为。
它存在的意义是让 legacy 能复用同一个 ToolExecutor，
而不是再写一套 LegacyToolExecutor。

    bot + event + config + ...
        ↓  LegacyToolExecutionContextAdapter
    AgentContext
        ↓
    ToolExecutor.execute()   ← 与 RUNTIME 同一个
"""

from __future__ import annotations

from typing import Any

from .context import AgentContext


def build_legacy_context(
    *,
    bot: Any,
    event: Any,
    config: Any,
    session_id: str,
    sender: str = "",
    intent_text: str = "",
    chat_text: str = "",
    interrupt: bool = False,
    extra: dict[str, Any] | None = None,
) -> AgentContext:
    """构造共享 ToolExecutor 需要的 AgentContext。

    Args:
        bot: OneBot Bot 实例。
        event: OneBot 事件（MessageEvent）。
        config: PluginConfig。
        session_id: 会话 id（`group:xxx` / `private:xxx`）。
        sender: 发送者显示名。
        intent_text: 用户意图文本（recovery 触发判定用）。
        chat_text: 已包装的聊天文本。
        interrupt: 是否插嘴模式。
        extra: 附加上下文（chat_text / reply_text 等工具会读的键）。
    """
    from ..gateway.context import from_onebot_event

    message = from_onebot_event(event, bot)
    if message is None:
        # 极端情况：事件类型无法识别。构造一个最小 context，
        # 让 ToolExecutor 能跑完（工具内部会因缺字段而失败，但不崩）。
        message = _minimal_message(session_id, intent_text)

    merged: dict[str, Any] = {
        "intent_text": intent_text,
        "chat_text": chat_text,
        "sender": sender,
    }
    if extra:
        merged.update(extra)
    # AgentContext.actor 由 message.actor 提供；这里保证不为空
    if not getattr(message, "actor", ""):
        try:
            message.actor = sender or str(getattr(event, "user_id", ""))
        except Exception:  # noqa: BLE001
            pass

    return AgentContext(
        session_id=session_id,
        message_context=message,
        trigger_reason="legacy",
        config=config,
        history=[],
        interrupt=interrupt,
        private_chat=bool(getattr(message, "is_private", False)),
        trace_id=getattr(message, "trace_id", "") or "",
        extra=merged,
    )


def _minimal_message(session_id: str, text: str) -> Any:
    from ..gateway.context import MessageContext

    return MessageContext(
        message_type="group",
        session_id=session_id,
        plain_text=text or "",
    )


class LegacyToolExecutionContextAdapter:
    """类形式（便于测试注入与将来的扩展）。

    行为与 `build_legacy_context` 一致，提供 `build()` 方法。
    """

    def build(self, **kwargs: Any) -> AgentContext:
        return build_legacy_context(**kwargs)


__all__ = [
    "build_legacy_context",
    "LegacyToolExecutionContextAdapter",
]
