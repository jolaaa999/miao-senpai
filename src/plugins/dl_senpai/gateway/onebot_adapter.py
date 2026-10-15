"""OneBot 适配器：原始 NoneBot 事件 → `MessageContext`。

这一层是 Gateway，职责**只有一个**：翻译。它不做路由决策、不做权限判断、
不调业务代码。这样做的价值是让"消息从哪来"和"消息该谁处理"彻底解耦。
"""

from __future__ import annotations

from typing import Any

from ..observability.context import current_trace, new_trace_id
from .context import (
    NOTICE_GROUP_DECREASE,
    NOTICE_GROUP_INCREASE,
    NOTICE_OTHER,
    MessageContext,
    session_id_for_group,
    session_id_for_private,
)

# 惰性 import：单测环境可能没有 nonebot adapter
try:
    from nonebot.adapters.onebot.v11 import (
        GroupDecreaseNoticeEvent,
        GroupIncreaseNoticeEvent,
        GroupMessageEvent,
        MessageEvent,
        NoticeEvent,
        PrivateMessageEvent,
    )
except Exception:  # noqa: BLE001
    GroupMessageEvent = PrivateMessageEvent = MessageEvent = None  # type: ignore[assignment,misc]
    NoticeEvent = GroupIncreaseNoticeEvent = GroupDecreaseNoticeEvent = None  # type: ignore[assignment,misc]


def _sender_display(event: Any) -> str:
    """取发送者显示名：优先群名片，其次昵称，兜底 QQ 号。

    与 `handlers._sender_name()` 的取值顺序保持一致，避免同一用户在
    路由日志与业务日志里显示成两个不同的名字。
    """
    user_id = getattr(event, "user_id", "")
    info = getattr(event, "sender", None)
    if info is not None:
        card = getattr(info, "card", None)
        if card:
            return str(card)
        nickname = getattr(info, "nickname", None)
        if nickname:
            return str(nickname)
    return str(user_id)


def _plain_text(event: Any) -> str:
    """取纯文本。OneBot 的 `get_plaintext()` 会剥掉 CQ 码但保留 @ 文本。"""
    try:
        return event.get_plaintext() or ""
    except Exception:  # noqa: BLE001
        return ""


def _is_tome(event: Any, bot: Any) -> bool:
    """是否 @ 了机器人。

    优先用 adapter 的 `is_tome()`（它会正确处理 @全体成员 等边界）；
    失败时回退到手工扫描 at 段，与 `handlers._message_ats_bot` 逻辑一致。
    """
    try:
        if event.is_tome():
            return True
    except Exception:  # noqa: BLE001
        pass
    self_id = str(getattr(bot, "self_id", "") or getattr(event, "self_id", "") or "")
    try:
        for seg in event.message:
            if seg.type == "at" and str(seg.data.get("qq") or "") == self_id:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _reply_id(event: Any) -> str:
    reply = getattr(event, "reply", None)
    if reply is None:
        return ""
    mid = getattr(reply, "message_id", None)
    return str(mid) if mid is not None else ""


def from_onebot_event(event: Any, bot: Any = None) -> MessageContext | None:
    """把任意 NoneBot 事件转成 `MessageContext`。

    无法识别的类型返回 None（由调用方决定是否忽略），而不是抛异常——
    群里会出现大量无关通知事件，让它们变成异常会污染日志。
    """
    if event is None:
        return None

    # 继承当前 trace（如果有），否则新建——保证每条消息都能被单独追踪
    ctx_trace = current_trace()
    trace = ctx_trace.trace_id if ctx_trace is not None else new_trace_id()

    base: dict[str, Any] = {
        "platform": "onebot.v11",
        "message_id": str(getattr(event, "message_id", "") or ""),
        "user_id": str(getattr(event, "user_id", "") or ""),
        "group_id": str(getattr(event, "group_id", "") or ""),
        "self_id": str(
            getattr(bot, "self_id", "") or getattr(event, "self_id", "") or ""
        ),
        "actor": _sender_display(event),
        "raw_message": getattr(event, "message", None),
        "raw_event": event,
        "bot": bot,
        "reply_to": _reply_id(event),
        "trace_id": trace,
    }

    if GroupMessageEvent is not None and isinstance(event, GroupMessageEvent):
        return MessageContext(
            **base,
            message_type="group",
            session_id=session_id_for_group(event.group_id),
            plain_text=_plain_text(event),
            is_to_me=_is_tome(event, bot),
            is_private=False,
        )

    if PrivateMessageEvent is not None and isinstance(event, PrivateMessageEvent):
        return MessageContext(
            **base,
            message_type="private",
            session_id=session_id_for_private(event.user_id),
            plain_text=_plain_text(event),
            # 私聊天然是"找我"，与 handlers.handle_private(force_mention=True) 一致
            is_to_me=True,
            is_private=True,
        )

    if NoticeEvent is not None and isinstance(event, NoticeEvent):
        notice_type = NOTICE_OTHER
        if GroupIncreaseNoticeEvent is not None and isinstance(
            event, GroupIncreaseNoticeEvent
        ):
            notice_type = NOTICE_GROUP_INCREASE
        elif GroupDecreaseNoticeEvent is not None and isinstance(
            event, GroupDecreaseNoticeEvent
        ):
            notice_type = NOTICE_GROUP_DECREASE
        gid = str(getattr(event, "group_id", "") or "")
        return MessageContext(
            **base,
            message_type="notice",
            # 通知没有"会话"语义，但给个稳定 key 方便日志聚合
            session_id=session_id_for_group(gid) if gid else "",
            plain_text="",
            is_to_me=False,
            is_private=False,
            notice_type=notice_type,
        )

    # 兜底：未知消息类事件（例如 MessageEvent 的其它子类）
    if MessageEvent is not None and isinstance(event, MessageEvent):
        return MessageContext(
            **base,
            message_type="unknown",
            session_id="",
            plain_text=_plain_text(event),
            is_to_me=_is_tome(event, bot),
            is_private=False,
        )

    return None


__all__ = ["from_onebot_event"]
