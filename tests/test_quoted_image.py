"""引用图（2026-10-15）：本轮无图时回退到被引用消息里的图。

背景：QQ 会把被引用的图折成 [图片]，本轮消息段里根本没有 image，
ctx.has_images() 为 False，于是「引用一张图问学姐」永远拿不到视觉输入，
模型只能看到「[图片]」三个字。回退带三道闸：开关 / 发送者 / 时效，逐条钉住。
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from dl_senpai import legacy_bridge as bridge
from dl_senpai import vision as vision_mod


def _cfg(**over) -> SimpleNamespace:
    """默认配置：三道闸都在保守侧（只认同发送者 + 学姐自己的图、120 秒、不认表情）。"""
    base = dict(
        vision_enable=True,
        vision_max_images=3,
        vision_quoted_enable=True,
        vision_quoted_same_sender_only=True,
        vision_quoted_ttl_seconds=120,
        vision_quoted_allow_mface=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _image_message(seg_type: str = "image", **data) -> Message:
    payload = {"file": "quoted.jpg"}
    payload.update(data)
    return Message(MessageSegment(seg_type, payload))


def _ctx(*, reply=None, raw_message=None, cfg=None, user_id="10001", self_id="999"):
    raw_event = SimpleNamespace(
        message=(
            raw_message
            if raw_message is not None
            else Message(MessageSegment.text("你看这张"))
        ),
        reply=reply,
    )
    return SimpleNamespace(
        extra={"config": cfg if cfg is not None else _cfg()},
        raw_event=raw_event,
        user_id=user_id,
        bot=SimpleNamespace(self_id=self_id),
        # 与 gateway/context.py 的 has_images 语义一致：只看本轮消息段
        has_images=lambda: any(
            seg.type in {"image", "mface"} for seg in raw_event.message
        ),
    )


def _reply(*, user_id="10001", age_seconds=0.0, message=None):
    return SimpleNamespace(
        sender=SimpleNamespace(user_id=user_id),
        time=time.time() - age_seconds,
        message=message if message is not None else _image_message(),
    )


async def _go(ctx, reply=None, reply_message="same"):
    """按真实调用点的方式传参：reply_message 由 resolve_reply_message 解析后传进来。

    reply_message 默认取 reply.message（正常路径）；显式传 None 用于覆盖
    「拿不到引用正文」这种异常路径。
    """
    msg = reply.message if (reply_message == "same" and reply is not None) else reply_message
    return await bridge._resolve_image_urls(ctx, reply_message=msg)


@pytest.fixture
def vision_calls(monkeypatch):
    """替掉真正的下载：记录被解析的消息段，返回可断言的假 data URL。"""
    calls: list[list[dict]] = []

    async def _fake(bot, segments, *, max_images):
        calls.append(list(segments))
        return [f"data:image/jpeg;base64,SEG{len(calls)}"]

    monkeypatch.setattr(vision_mod, "resolve_vision_images", _fake)
    return calls


@pytest.mark.asyncio
async def test_quoted_image_same_sender_reaches_vision(vision_calls) -> None:
    reply = _reply()
    urls = await _go(_ctx(reply=reply), reply=reply)
    assert urls == ["data:image/jpeg;base64,SEG1"]
    assert vision_calls[0][0]["file"] == "quoted.jpg"


@pytest.mark.asyncio
async def test_current_message_image_still_wins(vision_calls) -> None:
    """本轮自己带图时用本轮的，不做引用回退。"""
    raw = Message(MessageSegment("image", {"file": "mine.jpg"}))
    reply = _reply(user_id="20002")
    await _go(_ctx(reply=reply, raw_message=raw), reply=reply)
    assert vision_calls[0][0]["file"] == "mine.jpg"


@pytest.mark.asyncio
async def test_quoted_image_from_someone_else_is_ignored(vision_calls) -> None:
    """别人引用的群友图默认不认：费额度，而且内容多半跟本轮提问无关。"""
    reply = _reply(user_id="20002")
    assert await _go(_ctx(reply=reply), reply=reply) == []
    assert vision_calls == []


@pytest.mark.asyncio
async def test_quoted_bot_own_image_is_allowed(vision_calls) -> None:
    """引用学姐自己发过的图要认 —— 「这张图啥意思」是最常见的一类。"""
    reply = _reply(user_id="999")
    assert await _go(_ctx(reply=reply), reply=reply) != []


@pytest.mark.asyncio
async def test_quoted_image_expires(vision_calls) -> None:
    reply = _reply(age_seconds=121)
    assert await _go(_ctx(reply=reply), reply=reply) == []
    assert vision_calls == []


@pytest.mark.asyncio
async def test_quoted_image_ttl_zero_means_no_expiry(vision_calls) -> None:
    """ttl=0 = 关掉时效闸，只剩发送者闸。"""
    reply = _reply(age_seconds=86400)
    urls = await _go(_ctx(cfg=_cfg(vision_quoted_ttl_seconds=0), reply=reply), reply=reply)
    assert urls != []


@pytest.mark.asyncio
async def test_quoted_image_without_timestamp_is_still_used(vision_calls) -> None:
    """拿不到时间戳时不因为时效拦掉 —— 缺字段不等于过期。"""
    reply = _reply()
    reply.time = None
    assert await _go(_ctx(reply=reply), reply=reply) != []


@pytest.mark.asyncio
async def test_quoted_mface_is_ignored_by_default(vision_calls) -> None:
    reply = _reply(message=_image_message("mface", url="https://cdn/emoji.png"))
    assert await _go(_ctx(reply=reply), reply=reply) == []


@pytest.mark.asyncio
async def test_quoted_mface_allowed_when_switched_on(vision_calls) -> None:
    reply = _reply(message=_image_message("mface", url="https://cdn/emoji.png"))
    urls = await _go(
        _ctx(cfg=_cfg(vision_quoted_allow_mface=True), reply=reply), reply=reply
    )
    assert urls != []


@pytest.mark.asyncio
async def test_quoted_fallback_can_be_switched_off(vision_calls) -> None:
    reply = _reply()
    assert await _go(_ctx(cfg=_cfg(vision_quoted_enable=False), reply=reply), reply=reply) == []


@pytest.mark.asyncio
async def test_vision_disabled_ignores_quoted_too(vision_calls) -> None:
    """总开关关掉时引用图也不能漏进来。"""
    reply = _reply()
    assert await _go(_ctx(cfg=_cfg(vision_enable=False), reply=reply), reply=reply) == []


@pytest.mark.asyncio
async def test_reply_without_resolved_message_is_ignored(vision_calls) -> None:
    """resolve_reply_message 没拿到正文（reply_message=None）时不回退。"""
    reply = _reply()
    assert await _go(_ctx(reply=reply), reply=reply, reply_message=None) == []


@pytest.mark.asyncio
async def test_no_reply_keeps_old_behaviour(vision_calls) -> None:
    assert await bridge._resolve_image_urls(_ctx(reply=None)) == []


@pytest.mark.asyncio
async def test_unknown_sender_is_not_trusted(vision_calls) -> None:
    """拿不到发送者（异常事件）时按「不认识」处理，宁可当没图。"""
    reply = _reply()
    reply.sender = SimpleNamespace(user_id="")
    assert await _go(_ctx(reply=reply), reply=reply) == []


@pytest.mark.asyncio
async def test_non_image_segment_only_reply_is_empty(vision_calls) -> None:
    """引用的只是一句文字时不该触发视觉。"""
    msg = Message(MessageSegment.text("引用的是纯文字"))
    reply = _reply(message=msg)
    assert await _go(_ctx(reply=reply), reply=reply) == []


def test_deliver_path_passes_reply_message() -> None:
    """结构守卫：handle_agent_message 必须把解析好的引用消息传进去，
    否则回退逻辑永远收到 None，等于没写。"""
    import inspect

    src = inspect.getsource(bridge.handle_agent_message)
    assert "_resolve_image_urls(ctx, reply_message=reply_message)" in src
