from __future__ import annotations

import asyncio
import random
import re
from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11 import Message as OBMessage

from .persona import get_senpai_name

_PARA_SPLIT = re.compile(r"\n{2,}")
_LINE_SPLIT = re.compile(r"(?<=\n)")
_SENT_SPLIT = re.compile(r"(?<=[。！？.!?\n])")
_BUBBLE_SENT = re.compile(r"(?<=[。！？!?～~…])")


def split_reply_chunks(text: str, chunk_chars: int) -> list[str]:
    """把长回复拆成多段，优先按段落/行/句子切分。"""
    text = (text or "").strip()
    if not text:
        return []
    limit = max(80, chunk_chars)
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining.strip())
            break
        window = remaining[:limit]
        cut = _best_cut(window)
        if cut < max(40, limit // 4):
            cut = limit
        piece = remaining[:cut].rstrip()
        if piece:
            chunks.append(piece)
        remaining = remaining[cut:].lstrip()
    return [c for c in chunks if c]


def should_split_image_voice(
    image: MessageSegment | None,
    voice: MessageSegment | None,
) -> bool:
    """图片和语音同时存在时拆成独立消息，避免 NapCat 多附件上传失败。"""
    return image is not None and voice is not None


def split_human_bubbles(
    text: str,
    *,
    min_chars: int = 40,
    prefer_chars: int = 120,
    max_bubbles: int = 3,
) -> list[str]:
    """把闲聊短回复拆成多条 QQ 气泡（像真人连发）。

    太短不拆；超长交给合并转发路径，这里只处理「刚好能碎成 2～N 句」的情况。
    """
    text = (text or "").strip()
    if not text:
        return []
    max_bubbles = max(1, int(max_bubbles))
    min_chars = max(1, int(min_chars))
    prefer_chars = max(min_chars, int(prefer_chars))
    if len(text) < min_chars or max_bubbles <= 1:
        return [text]

    # 优先空行，其次单换行，再按句末
    raw_parts: list[str] = []
    if "\n\n" in text:
        raw_parts = [p.strip() for p in _PARA_SPLIT.split(text) if p.strip()]
    elif "\n" in text:
        raw_parts = [p.strip() for p in text.split("\n") if p.strip()]
    else:
        raw_parts = [p.strip() for p in _BUBBLE_SENT.split(text) if p.strip()]
        if len(raw_parts) <= 1:
            return [text]

    # 合并过碎片段，避免一字一气泡
    merged: list[str] = []
    buf = ""
    soft_limit = max(20, prefer_chars // 2)
    for part in raw_parts:
        if not buf:
            buf = part
            continue
        if len(buf) < soft_limit and len(buf) + len(part) <= prefer_chars:
            buf = f"{buf}{part}"
        else:
            merged.append(buf)
            buf = part
    if buf:
        merged.append(buf)

    if len(merged) <= 1:
        return [text]

    # 仍过多则从前合并，保留末尾情绪句
    while len(merged) > max_bubbles:
        merged[0] = merged[0] + merged[1]
        del merged[1]
    return [m for m in merged if m]


def _best_cut(window: str) -> int:
    for pattern in (_PARA_SPLIT, _LINE_SPLIT, _SENT_SPLIT):
        matches = list(pattern.finditer(window))
        if matches:
            return matches[-1].end()
    space = window.rfind(" ")
    if space > len(window) // 3:
        return space + 1
    return len(window)


def _segment_file_hint(segment: MessageSegment) -> str:
    data = segment.data if isinstance(segment.data, dict) else {}
    hint = str(data.get("file") or data.get("url") or "").strip()
    if hint:
        return hint[:96]
    return segment.type


def _reply_media_summary(
    *,
    sticker: MessageSegment | None,
    voice: MessageSegment | None,
    image: MessageSegment | None,
    split_media: bool,
) -> str:
    parts: list[str] = []
    if sticker is not None:
        parts.append("sticker")
    if image is not None:
        parts.append("image" + ("(split)" if split_media else ""))
    if voice is not None:
        parts.append("voice" + ("(split)" if split_media else ""))
    return ",".join(parts) if parts else "text-only"


def build_forward_nodes(
    chunks: list[str],
    *,
    user_id: int,
    nickname: str | None = None,
    sticker: MessageSegment | None = None,
    image: MessageSegment | None = None,
) -> list[MessageSegment]:
    nickname = nickname or get_senpai_name()
    nodes: list[MessageSegment] = []
    for chunk in chunks:
        nodes.append(
            MessageSegment.node_custom(
                user_id=user_id,
                nickname=nickname,
                content=OBMessage(chunk),
            )
        )
    if image is not None:
        nodes.append(
            MessageSegment.node_custom(
                user_id=user_id,
                nickname=nickname,
                content=OBMessage(image),
            )
        )
    if sticker is not None:
        nodes.append(
            MessageSegment.node_custom(
                user_id=user_id,
                nickname=nickname,
                content=OBMessage(sticker),
            )
        )
    return nodes


async def _send_image_with_retry(
    bot: Bot,
    event: MessageEvent,
    image: MessageSegment,
) -> bool:
    hint = _segment_file_hint(image)
    for attempt in range(1, 3):
        try:
            await bot.send(event, OBMessage(image))
            logger.info(
                f"dl_senpai: reply image sent ok attempt={attempt}/2 file={hint!r}"
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"dl_senpai: reply image send failed attempt={attempt}/2 "
                f"file={hint!r}: {exc}"
            )
    logger.error(f"dl_senpai: reply image send give up file={hint!r}")
    return False


async def _send_voice_segment(
    bot: Bot,
    event: MessageEvent,
    voice: MessageSegment,
) -> bool:
    hint = _segment_file_hint(voice)
    try:
        await bot.send(event, OBMessage(voice))
        logger.info(f"dl_senpai: reply voice sent ok file={hint!r}")
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai: reply voice send failed file={hint!r}: {exc}")
        return False


async def send_text_reply(
    bot: Bot,
    event: MessageEvent,
    text: str,
    *,
    sticker: MessageSegment | None = None,
    voice: MessageSegment | None = None,
    image: MessageSegment | None = None,
    reply_max_chars: int,
    reply_chunk_chars: int,
    nickname: str | None = None,
    humanize_send: bool = False,
    typing_delay_ms_min: int = 400,
    typing_delay_ms_max: int = 1200,
    bubble_max: int = 3,
    bubble_min_chars: int = 40,
    bubble_prefer_chars: int = 120,
) -> None:
    nickname = nickname or get_senpai_name()
    """短消息直发；超长则用合并转发（聊天记录）多段发送。

    开启 humanize_send 时，中等长度闲聊会拆成多气泡并带打字延迟。
    图片与语音同时存在时拆成独立消息；图片发送失败会单独重试一次。
    正文带媒体发送失败时自动降级为纯文字，并尽量补发图片/语音。
    """
    text = (text or "").strip()
    if not text and sticker is None and voice is None and image is None:
        return

    split_media = should_split_image_voice(image, voice)
    bundled_image = None if split_media else image
    bundled_voice = None if split_media else voice
    deferred_image = image if split_media else None
    deferred_voice = voice if split_media else None
    media_summary = _reply_media_summary(
        sticker=sticker,
        voice=voice,
        image=image,
        split_media=split_media,
    )

    try:
        await _send_text_reply_once(
            bot,
            event,
            text,
            sticker=sticker,
            voice=bundled_voice,
            image=bundled_image,
            reply_max_chars=reply_max_chars,
            reply_chunk_chars=reply_chunk_chars,
            nickname=nickname,
            humanize_send=humanize_send,
            typing_delay_ms_min=typing_delay_ms_min,
            typing_delay_ms_max=typing_delay_ms_max,
            bubble_max=bubble_max,
            bubble_min_chars=bubble_min_chars,
            bubble_prefer_chars=bubble_prefer_chars,
        )
        logger.info(f"dl_senpai: reply body sent ok media={media_summary}")
    except Exception as exc:  # noqa: BLE001
        if sticker is None and bundled_image is None and bundled_voice is None:
            raise
        logger.warning(
            f"dl_senpai: reply body send failed, fallback text-only "
            f"media={media_summary}: {exc}"
        )
        await _send_text_reply_once(
            bot,
            event,
            text,
            sticker=None,
            voice=None,
            image=None,
            reply_max_chars=reply_max_chars,
            reply_chunk_chars=reply_chunk_chars,
            nickname=nickname,
            humanize_send=humanize_send,
            typing_delay_ms_min=typing_delay_ms_min,
            typing_delay_ms_max=typing_delay_ms_max,
            bubble_max=bubble_max,
            bubble_min_chars=bubble_min_chars,
            bubble_prefer_chars=bubble_prefer_chars,
        )
        logger.info("dl_senpai: reply text-only fallback sent ok")
        if bundled_image is not None:
            await _send_image_with_retry(bot, event, bundled_image)
        if bundled_voice is not None:
            await _send_voice_segment(bot, event, bundled_voice)

    if deferred_image is not None:
        await _send_image_with_retry(bot, event, deferred_image)
    if deferred_voice is not None:
        await _send_voice_segment(bot, event, deferred_voice)


async def _typing_pause(ms_min: int, ms_max: int) -> None:
    lo = max(0, int(ms_min))
    hi = max(lo, int(ms_max))
    delay_ms = random.randint(lo, hi) if hi > 0 else 0
    if delay_ms > 0:
        await asyncio.sleep(delay_ms / 1000.0)


async def _send_text_reply_once(
    bot: Bot,
    event: MessageEvent,
    text: str,
    *,
    sticker: MessageSegment | None,
    voice: MessageSegment | None,
    image: MessageSegment | None,
    reply_max_chars: int,
    reply_chunk_chars: int,
    nickname: str,
    humanize_send: bool = False,
    typing_delay_ms_min: int = 400,
    typing_delay_ms_max: int = 1200,
    bubble_max: int = 3,
    bubble_min_chars: int = 40,
    bubble_prefer_chars: int = 120,
) -> None:
    threshold = max(1, reply_max_chars)

    # 拟人碎句：仅短于合并转发阈值的正文
    if (
        humanize_send
        and text
        and len(text) <= threshold
    ):
        bubbles = split_human_bubbles(
            text,
            min_chars=bubble_min_chars,
            prefer_chars=bubble_prefer_chars,
            max_bubbles=bubble_max,
        )
        if len(bubbles) >= 2:
            await _typing_pause(typing_delay_ms_min // 2, typing_delay_ms_min)
            for i, chunk in enumerate(bubbles):
                if i > 0:
                    await _typing_pause(typing_delay_ms_min, typing_delay_ms_max)
                piece = OBMessage(chunk)
                last = i == len(bubbles) - 1
                if last and image is not None:
                    piece = piece + OBMessage(image)
                if last and sticker is not None:
                    piece = piece + OBMessage(sticker)
                if last and voice is not None:
                    piece = piece + OBMessage(voice)
                await bot.send(event, piece)
            logger.info(f"dl_senpai: humanize sent {len(bubbles)} bubble(s)")
            return

    if not text or len(text) <= threshold:
        if humanize_send and text:
            await _typing_pause(typing_delay_ms_min // 2, typing_delay_ms_min)
        msg = OBMessage(text) if text else OBMessage()
        if image is not None:
            msg = msg + OBMessage(image)
        if sticker is not None:
            msg = msg + OBMessage(sticker)
        if voice is not None:
            msg = msg + OBMessage(voice)
        if not msg:
            return
        await bot.send(event, msg)
        return

    chunks = split_reply_chunks(text, reply_chunk_chars)
    if len(chunks) <= 1:
        msg = OBMessage(text)
        if image is not None:
            msg = msg + OBMessage(image)
        if sticker is not None:
            msg = msg + OBMessage(sticker)
        if voice is not None:
            msg = msg + OBMessage(voice)
        await bot.send(event, msg)
        return

    nodes = build_forward_nodes(
        chunks,
        user_id=int(bot.self_id),
        nickname=nickname,
        sticker=sticker,
        image=image,
    )
    try:
        await _send_forward(bot, event, nodes)
        if voice is not None:
            await bot.send(event, OBMessage(voice))
        logger.info(f"dl_senpai: sent forward reply with {len(nodes)} node(s)")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai: forward reply failed, fallback sequential: {exc}")
        for i, chunk in enumerate(chunks):
            piece = OBMessage(chunk)
            if sticker is not None and i == len(chunks) - 1:
                piece = piece + OBMessage(sticker)
            await bot.send(event, piece)
        if voice is not None:
            await bot.send(event, OBMessage(voice))


async def _send_forward(bot: Bot, event: MessageEvent, nodes: list[MessageSegment]) -> Any:
    if isinstance(event, GroupMessageEvent):
        return await bot.send_group_forward_msg(group_id=event.group_id, messages=nodes)
    if isinstance(event, PrivateMessageEvent):
        return await bot.send_private_forward_msg(user_id=event.user_id, messages=nodes)
    # 兜底：当成普通消息拼接发送
    return await bot.send(event, OBMessage(nodes))
