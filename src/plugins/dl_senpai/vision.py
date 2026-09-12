from __future__ import annotations

import base64
import re
from typing import Any

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

_STICKER_IMAGE_SUB_TYPES = {1, "1", "sticker", "emoji"}
_STICKER_SUMMARY_HINTS = ("[动画表情]", "[表情]", "emoji", "sticker")
_DATA_URL_RE = re.compile(r"^data:image/[\w+.-]+;base64,", re.I)
_RESTRICTED_CDN_MARKERS = (
    "qq.com",
    "qpic.cn",
    "gtimg.com",
    "myqcloud.com",
)


def _is_restricted_cdn_url(url: str) -> bool:
    lower = url.lower()
    return any(marker in lower for marker in _RESTRICTED_CDN_MARKERS)


def _extract_base64(resp: Any) -> str | None:
    if isinstance(resp, dict):
        if resp.get("base64"):
            return str(resp["base64"])
        data = resp.get("data")
        if isinstance(data, dict) and data.get("base64"):
            return str(data["base64"])
    b64 = getattr(resp, "base64", None)
    if b64:
        return str(b64)
    data = getattr(resp, "data", None)
    if data is not None:
        if isinstance(data, dict) and data.get("base64"):
            return str(data["base64"])
        nested = getattr(data, "base64", None)
        if nested:
            return str(nested)
    return None


def is_sticker_image(data: dict[str, Any]) -> bool:
    sub_type = data.get("sub_type")
    if sub_type in _STICKER_IMAGE_SUB_TYPES:
        return True
    summary = str(data.get("summary", "")).lower()
    return any(hint.lower() in summary for hint in _STICKER_SUMMARY_HINTS)


def extract_vision_image_segments(message: Any) -> list[dict[str, Any]]:
    """提取可用于识图的消息段：普通图片、贴纸图、mface。不含 QQ 小黄脸 face。"""
    found: list[dict[str, Any]] = []
    if message is None:
        return found
    for seg in message:
        seg_type = getattr(seg, "type", None)
        data = dict(getattr(seg, "data", {}) or {})
        if seg_type == "image":
            found.append(data)
        elif seg_type == "mface":
            if data.get("url") or data.get("file") or data.get("key"):
                found.append(data)
    return found


def _normalize_data_url(raw: str) -> str:
    cleaned = raw.strip()
    if _DATA_URL_RE.match(cleaned):
        return cleaned
    if cleaned.startswith("base64://"):
        cleaned = cleaned.removeprefix("base64://")
    return f"data:image/jpeg;base64,{cleaned}"


async def _fetch_image_base64(bot: Bot, file_ref: str) -> str | None:
    try:
        resp = await bot.get_image(file=file_ref)
        b64 = _extract_base64(resp)
        if b64:
            return _normalize_data_url(b64)
        logger.warning(f"dl_senpai: get_image returned empty base64 file={file_ref[:48]}")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai: get_image failed file={file_ref[:48]} err={exc}")
    return None


async def _download_image_as_data_url(url: str) -> str | None:
    """Bot 本机通常能访问 QQ CDN；下载后转 base64 给中转站。"""
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
            if not content_type.startswith("image/"):
                content_type = "image/jpeg"
            encoded = base64.b64encode(resp.content).decode("ascii")
            return f"data:{content_type};base64,{encoded}"
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai: download image failed url={url[:72]} err={exc}")
    return None


async def resolve_image_data_url(bot: Bot, data: dict[str, Any]) -> str | None:
    file_ref = str(data.get("file") or "").strip()
    url = str(data.get("url") or "").strip()

    if file_ref.startswith("base64://"):
        return _normalize_data_url(file_ref.removeprefix("base64://"))
    if file_ref.startswith("data:image"):
        return file_ref

    # 1) OneBot get_image（NapCat 本地缓存）
    if file_ref and not file_ref.startswith(("http://", "https://")):
        resolved = await _fetch_image_base64(bot, file_ref)
        if resolved:
            return resolved

    # 2) 本机下载 QQ CDN（中转站拉不到，Bot 这边可以）
    if url.startswith(("http://", "https://")):
        resolved = await _download_image_as_data_url(url)
        if resolved:
            return resolved

    # 3) 公网 URL 兜底
    for candidate in (url, file_ref):
        if not candidate.startswith(("http://", "https://")):
            continue
        if _is_restricted_cdn_url(candidate):
            continue
        return candidate

    if file_ref:
        return await _fetch_image_base64(bot, file_ref)
    return None


async def resolve_vision_images(
    bot: Bot,
    segments: list[dict[str, Any]],
    *,
    max_images: int,
) -> list[str]:
    urls: list[str] = []
    for data in segments[: max(1, max_images)]:
        resolved = await resolve_image_data_url(bot, data)
        if resolved:
            urls.append(resolved)
        else:
            logger.warning(
                "dl_senpai: vision segment unresolved "
                f"file={str(data.get('file', ''))[:48]} url={str(data.get('url', ''))[:48]}"
            )
    if segments:
        logger.info(f"dl_senpai: vision resolved {len(urls)}/{len(segments[:max_images])} image(s)")
    return urls


def build_user_content(text: str, image_urls: list[str] | None) -> str | list[dict[str, Any]]:
    if not image_urls:
        return text
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for url in image_urls:
        parts.append({"type": "image_url", "image_url": {"url": url}})
    return parts


def format_user_text_for_chat(
    text: str,
    *,
    image_count: int = 0,
    sticker_count: int = 0,
) -> str:
    """把用户消息整理成喂给模型的文字。

    注意：纯表情/贴纸时 plaintext 常为空，绝不能再默认成「你好」。
    不对黄豆/小黄脸等表情包做识图或情绪解读，只标注发了表情。
    """
    stripped = text.strip()
    if stripped:
        return stripped
    if image_count > 0:
        if sticker_count > 0:
            if sticker_count == 1:
                return "（发了张表情，请看看）"
            return f"（发了{sticker_count}张表情，请看看）"
        if image_count == 1:
            return "（发了一张图，请看看）"
        return f"（发了{image_count}张图，请看看）"
    if sticker_count > 0:
        if sticker_count == 1:
            return "（发了个表情）"
        return f"（发了{sticker_count}个表情）"
    return ""


def format_user_text_for_memory(
    sender: str,
    text: str,
    *,
    image_count: int = 0,
    sticker_count: int = 0,
) -> str:
    body = format_user_text_for_chat(
        text,
        image_count=image_count,
        sticker_count=sticker_count,
    )
    if image_count > 0 and sticker_count <= 0:
        suffix = f" [附{image_count}张图]"
        return f"{sender}：{body}{suffix}"
    if sticker_count > 0 and "表情" not in body:
        return f"{sender}：{body} [表情]"
    return f"{sender}：{body}" if body else f"{sender}：（空消息）"
