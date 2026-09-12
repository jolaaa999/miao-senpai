from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from dl_senpai.reply_quote import (
    build_chat_text_with_reply,
    extract_message_plaintext,
    format_with_reply_quote,
    get_reply_message,
    reply_sender_name,
    resolve_reply_message,
)


def test_extract_message_plaintext() -> None:
    msg = Message(
        MessageSegment.text("今晚开卷吗")
        + MessageSegment.face(id_=178)
        + MessageSegment.image(file="a.jpg")
    )
    text = extract_message_plaintext(msg)
    assert "今晚开卷吗" in text
    assert "小黄脸" not in text
    assert "[图片]" in text


def test_format_with_reply_quote() -> None:
    text = format_with_reply_quote(
        "这题怎么做",
        reply_sender="小明",
        reply_text="求一道二分答案",
    )
    assert "【引用消息】小明：求一道二分答案" in text
    assert "【我现在说的】这题怎么做" in text


def test_format_with_reply_quote_empty_user() -> None:
    text = format_with_reply_quote(
        "",
        reply_sender="小红",
        reply_text="你们看这个",
    )
    assert "小红：你们看这个" in text
    assert "没另打字" in text


def test_build_chat_text_with_reply() -> None:
    reply = SimpleNamespace(
        sender=SimpleNamespace(card="卷王", nickname="小明", user_id=1),
        message=Message(MessageSegment.text("过拟合是啥")),
    )
    event = SimpleNamespace(reply=reply)
    out = build_chat_text_with_reply(event, "用人话讲讲")
    assert "卷王：过拟合是啥" in out
    assert "用人话讲讲" in out


def test_build_chat_text_without_reply() -> None:
    event = SimpleNamespace(reply=None)
    assert build_chat_text_with_reply(event, "你好") == "你好"


def test_reply_sender_name_fallback() -> None:
    reply = SimpleNamespace(
        sender=SimpleNamespace(card="", nickname="", user_id=42)
    )
    assert reply_sender_name(reply) == "42"


def test_get_reply_message_extracts_vision_images() -> None:
    from dl_senpai.vision import extract_vision_image_segments

    reply = SimpleNamespace(
        sender=SimpleNamespace(card="小明", nickname="", user_id=1),
        message=Message(
            MessageSegment.text("看图")
            + MessageSegment("image", {"file": "quoted.jpg", "sub_type": 0})
        ),
    )
    event = SimpleNamespace(reply=reply)
    reply_msg = get_reply_message(event)
    assert reply_msg is not None
    segs = extract_vision_image_segments(reply_msg)
    assert len(segs) == 1
    assert segs[0]["file"] == "quoted.jpg"
    assert "看图" in extract_message_plaintext(reply_msg)
    assert "[图片]" in extract_message_plaintext(reply_msg)


@pytest.mark.asyncio
async def test_resolve_reply_message_fetches_when_quote_lacks_image_segment() -> None:
    cached = Message(MessageSegment.text("[图片]"))
    full = Message(MessageSegment("image", {"file": "quoted.jpg", "sub_type": 0}))
    reply = SimpleNamespace(
        message_id=999,
        message=cached,
        sender=SimpleNamespace(card="小明", nickname="", user_id=1),
    )
    event = SimpleNamespace(reply=reply)
    bot = AsyncMock()
    bot.get_msg = AsyncMock(return_value={"message": full})

    resolved = await resolve_reply_message(bot, event)
    assert resolved is not None
    bot.get_msg.assert_awaited_once_with(message_id=999)
    from dl_senpai.vision import extract_vision_image_segments

    segs = extract_vision_image_segments(resolved)
    assert len(segs) == 1
    assert segs[0]["file"] == "quoted.jpg"
