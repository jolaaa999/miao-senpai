from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from dl_senpai.vision import (
    build_user_content,
    extract_vision_image_segments,
    format_user_text_for_chat,
    format_user_text_for_memory,
    is_sticker_image,
    resolve_image_data_url,
)


def test_is_sticker_image() -> None:
    assert is_sticker_image({"sub_type": 1}) is True
    assert is_sticker_image({"summary": "[动画表情]"}) is True
    assert is_sticker_image({"sub_type": 0}) is False


def test_extract_vision_image_segments() -> None:
    msg = Message(
        MessageSegment("image", {"file": "photo.jpg", "sub_type": 0})
        + MessageSegment("image", {"file": "sticker.jpg", "sub_type": 1})
        + MessageSegment.face(id_=1)
    )
    found = extract_vision_image_segments(msg)
    assert len(found) == 2
    assert found[0]["file"] == "photo.jpg"
    assert found[1]["file"] == "sticker.jpg"


def test_extract_vision_image_segments_includes_mface() -> None:
    msg = Message(
        MessageSegment(
            "mface",
            {"url": "https://example.com/sticker.gif", "summary": "[动画表情]"},
        )
    )
    found = extract_vision_image_segments(msg)
    assert len(found) == 1
    assert found[0]["url"] == "https://example.com/sticker.gif"


def test_build_user_content_with_images() -> None:
    content = build_user_content("看看这个", ["https://example.com/a.jpg"])
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["image_url"]["url"] == "https://example.com/a.jpg"


def test_format_user_text() -> None:
    assert format_user_text_for_chat("", image_count=1) == "（发了一张图，请看看）"
    assert format_user_text_for_chat("", image_count=0) == ""
    assert format_user_text_for_chat("", sticker_count=1) == "（发了个表情）"
    assert format_user_text_for_chat("", sticker_count=2) == "（发了2个表情）"
    assert (
        format_user_text_for_chat("", sticker_count=1, image_count=1)
        == "（发了张表情，请看看）"
    )
    assert format_user_text_for_chat("哈哈", sticker_count=1) == "哈哈"
    assert "你好" not in format_user_text_for_chat("", sticker_count=2)
    assert "[附2张图]" in format_user_text_for_memory("小明", "帮我看", image_count=2)


@pytest.mark.asyncio
async def test_resolve_image_data_url_from_public_http() -> None:
    bot = MagicMock()
    url = await resolve_image_data_url(bot, {"url": "https://example.com/a.png"})
    assert url == "https://example.com/a.png"
    bot.get_image.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_qq_image_uses_base64_not_cdn_url() -> None:
    bot = MagicMock()
    bot.get_image = AsyncMock(return_value={"base64": "abc123"})
    url = await resolve_image_data_url(
        bot,
        {
            "file": "8CCD50B29BB5F2D2E10506A37D8184BC.jpg",
            "url": "https://multimedia.nt.qq.com.cn/download?appid=1406&fileid=xxx",
        },
    )
    assert url == "data:image/jpeg;base64,abc123"
    bot.get_image.assert_called_once_with(file="8CCD50B29BB5F2D2E10506A37D8184BC.jpg")


@pytest.mark.asyncio
async def test_resolve_qq_image_download_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bot = MagicMock()
    bot.get_image = AsyncMock(return_value={})

    async def fake_download(url: str) -> str:
        return "data:image/jpeg;base64,downloaded"

    monkeypatch.setattr("dl_senpai.vision._download_image_as_data_url", fake_download)
    url = await resolve_image_data_url(
        bot,
        {
            "file": "8CCD50B29BB5F2D2E10506A37D8184BC.jpg",
            "url": "https://multimedia.nt.qq.com.cn/download?appid=1406&fileid=xxx",
        },
    )
    assert url == "data:image/jpeg;base64,downloaded"


@pytest.mark.asyncio
async def test_resolve_image_data_url_from_file() -> None:
    bot = MagicMock()
    bot.get_image = AsyncMock(return_value={"base64": "abc123"})
    url = await resolve_image_data_url(bot, {"file": "abc1234"})
    assert url == "data:image/jpeg;base64,abc123"
