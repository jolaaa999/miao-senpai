"""QQ 引用/回复消息：拼进学姐可见上下文。"""

from __future__ import annotations

from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import Message


def user_intent_text(plain: str, chat_text: str = "") -> str:
    """用户本条发言（不含引用块），用于搜网/生图/意图识别。"""
    p = (plain or "").strip()
    if p:
        return p
    raw = (chat_text or "").strip()
    if "【我现在说的】" in raw:
        mine = raw.split("【我现在说的】", 1)[1].strip()
        if mine and mine != "（只引用了上面这条，自己没另打字）":
            return mine
    return raw


def get_reply_message(event: Any) -> Any | None:
    """event.reply 里被引用的原始 Message，无引用时返回 None。"""
    reply = getattr(event, "reply", None)
    if reply is None:
        return None
    return getattr(reply, "message", None)


def _reply_plaintext_suggests_media(message: Any) -> bool:
    plain = extract_message_plaintext(message)
    if not plain:
        return False
    hints = ("[图片]", "[动画表情]")
    return any(hint in plain for hint in hints)


async def resolve_reply_message(bot: Any, event: Any) -> Any | None:
    """尽量拿到引用消息的完整 Message（必要时 get_msg 拉原消息）。"""
    reply = getattr(event, "reply", None)
    if reply is None:
        return None

    cached = getattr(reply, "message", None)
    if cached is not None and not _reply_plaintext_suggests_media(cached):
        return cached

    from .vision import extract_vision_image_segments

    if cached is not None and extract_vision_image_segments(cached):
        return cached

    message_id = getattr(reply, "message_id", None)
    if message_id is None or bot is None:
        return cached

    try:
        result = await bot.get_msg(message_id=int(message_id))
        raw = result.get("message") if isinstance(result, dict) else None
        if not raw:
            return cached
        fetched = Message(raw)
        if extract_vision_image_segments(fetched) or not _reply_plaintext_suggests_media(
            fetched
        ):
            logger.info(f"dl_senpai: resolved quoted message via get_msg id={message_id}")
            return fetched
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai: get_msg for quote failed id={message_id} err={exc}")
    return cached


def extract_message_plaintext(message: Any) -> str:
    """从 OneBot Message 抽可读文本（含表情/图片占位）。"""
    if message is None:
        return ""
    parts: list[str] = []
    for seg in message:
        seg_type = getattr(seg, "type", None)
        data = dict(getattr(seg, "data", {}) or {})
        if seg_type == "text":
            parts.append(str(data.get("text") or ""))
        elif seg_type == "face":
            # QQ 黄豆/小黄脸：不向模型展示
            continue
        elif seg_type == "mface":
            summary = str(data.get("summary") or "").strip()
            parts.append(summary if summary else "[动画表情]")
        elif seg_type == "image":
            summary = str(data.get("summary") or "").strip()
            parts.append(summary if summary else "[图片]")
        elif seg_type == "at":
            qq = str(data.get("qq") or "").strip()
            if qq and qq != "all":
                name = str(data.get("name") or "").strip()
                parts.append(f"@{name}" if name else f"@{qq}")
            elif qq == "all":
                parts.append("@全体成员")
        elif seg_type == "reply":
            continue
        elif seg_type:
            parts.append(f"[{seg_type}]")
    return "".join(parts).strip()


def reply_sender_name(reply: Any) -> str:
    """引用消息发送者显示名。"""
    sender = getattr(reply, "sender", None)
    if sender is None:
        return "某人"
    card = getattr(sender, "card", None)
    nickname = getattr(sender, "nickname", None)
    user_id = getattr(sender, "user_id", None)
    return (card or nickname or (str(user_id) if user_id is not None else "") or "某人").strip()


def format_with_reply_quote(
    user_text: str,
    *,
    reply_sender: str,
    reply_text: str,
) -> str:
    """把「引用内容 + 我现在说的」合成喂给模型的一段话。"""
    quoted = (reply_text or "").strip() or "（非文字内容/未能解析）"
    who = (reply_sender or "某人").strip() or "某人"
    mine = (user_text or "").strip()
    if not mine:
        mine = "（只引用了上面这条，自己没另打字）"
    return (
        f"【引用消息】{who}：{quoted}\n"
        f"【我现在说的】{mine}"
    )


def build_chat_text_with_reply(
    event: Any,
    user_text: str,
    *,
    reply_message: Any | None = None,
) -> str:
    """若 event 带 reply，则拼引用上下文；否则原样返回 user_text。"""
    reply = getattr(event, "reply", None)
    if reply is None:
        return user_text
    try:
        reply_msg = (
            reply_message
            if reply_message is not None
            else getattr(reply, "message", None)
        )
        reply_text = extract_message_plaintext(reply_msg)
        sender = reply_sender_name(reply)
        return format_with_reply_quote(
            user_text,
            reply_sender=sender,
            reply_text=reply_text,
        )
    except Exception:  # noqa: BLE001
        return user_text
