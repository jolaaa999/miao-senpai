"""自画像：发一张她自己的图（对应 qq-bridge 的 qq_get_self_image）。

为什么需要它（2026-10-27）：

群里迟早有人问「你长什么样」「发张自拍」。此前学姐只有两条路：编一段描述，
或者用生图模型现画一张——现画的问题很明显：每次都不一样，等于每次换个人，
群里立刻会有人说「上次不是长这样」。

qq-bridge 的做法是 get_self_image：直接给一张**固定的**自己。这边同样：
素材放在 data/dl_senpai/self/ 里，她挑一张发出去，人设是稳定的。

三条约束：
1. **只能发自己**：这个工具不接 QQ 号参数，永远指她自己。
2. **不主动晒**：没人问就别发。真人不会突然把自己的照片甩进群里。
3. **不解释是哪来的**：不说「这是我的设定图」「这是角色参考图」——那句话
   一出口人设就塌了。发出去就是发出去。

素材目录为空时，工具会明确告诉模型「没有图」，并让它自然带过（说「我没有
照片」或者岔开话题都行），而不是逼它现编。
"""

from __future__ import annotations

import random
import re
import time
from pathlib import Path
from typing import Any, Callable

from .agent.tools.models import Continuation, ToolCall, ToolResult
from .agent.tools.registry import FailurePolicy, ToolDefinition
from .observability.logging import log_event

PORTRAIT_MARKER = "SELFIMAGE"

_PORTRAIT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_MARKER_RE = re.compile(r"<<<\s*SELFIMAGE\s*([^>\n]*)>>>", re.IGNORECASE)

PORTRAIT_PROMPT_TEXT = """【你自己长什么样】
有人问「你长什么样」「有没有照片」「发张你的图」，可以发一张自己的：

    <<<SELFIMAGE>>>

想挑特定的一张就在后面写关键词：<<<SELFIMAGE 全身>>>

什么时候发：别人真的在看你的样子、聊到长相、或者要你「发一张」。
什么时候别发：没人问的时候不要主动甩图；被夸一句就发图也过分。
别人问「这是真人吗」时不要用这个工具答——发图回答不了那个问题。

发完不要在文字里解释这是什么图、谁画的、什么设定。发出去就完了。
"""


def portrait_dir(config: Any = None) -> Path:
    raw = getattr(config, "self_image_dir", "") or "data/dl_senpai/self"
    return Path(str(raw))


def list_portraits(config: Any = None) -> list[Path]:
    """列出可用素材（只认图片后缀）。目录不存在就返回空。"""
    directory = portrait_dir(config)
    try:
        if not directory.is_dir():
            return []
        return sorted(
            path
            for path in directory.iterdir()
            # 排除归一化产物：*.norm.jpg 是出站前压出来的副本，
            # 混进候选池会让「同一张图出现两次」，随机挑时概率还翻倍。
            if path.is_file()
            and path.suffix.lower() in _PORTRAIT_SUFFIXES
            and ".norm." not in path.name
        )
    except OSError:
        return []


def pick_portrait(
    config: Any = None,
    hint: str = "",
    *,
    rng: Callable[[list[Path]], Any] = random.choice,
) -> Path | None:
    """挑一张。hint 命中文件名优先；命中不到就随机——不是报错。

    为什么 hint 未命中还要随机：模型写的关键词经常是「全身」「侧脸」这种
    素材里根本没有的说法，为这个失败等于把工具变成不可用。
    """
    portraits = list_portraits(config)
    if not portraits:
        return None
    token = str(hint or "").strip()
    if token:
        matched = [p for p in portraits if token in p.stem]
        if matched:
            return rng(matched)
    return rng(portraits)


def resolve_portrait_bytes(config: Any = None) -> bytes | None:
    """角色素材库兜底：配了 self_character 且库里有已核验的图时取一张。

    为什么留着这条兜底：character_references 里已经有整套「已核验参考图」
    的机制，用户在那边放图的话（生图用的同一套素材），自画像就不必再单独
    维护一份目录。
    """
    name = str(getattr(config, "self_character", "") or "").strip()
    if not name:
        return None
    try:
        from .character_references import resolve_library_reference
        from .character_registry import get_character_registry

        profile = get_character_registry(config).resolve(name)
        if profile is None:
            return None
        candidate = resolve_library_reference(config, profile)
        return candidate.raw if candidate is not None and candidate.raw else None
    except Exception:  # noqa: BLE001 — 兜底失败不构成错误，上游还有目录这条主路
        return None


def parse_portrait_request(raw_reply: str) -> tuple[str, list[ToolCall]]:
    text = raw_reply or ""
    calls: list[ToolCall] = []
    for match in _MARKER_RE.finditer(text):
        calls.append(
            ToolCall(
                tool_name="self_image",
                arguments={"hint": (match.group(1) or "").strip()},
                raw=match.group(0),
                marker=PORTRAIT_MARKER,
            )
        )
    return _MARKER_RE.sub("", text), calls


async def execute_self_image(context: Any, call: ToolCall) -> ToolResult:
    """发一张她自己的图。素材没有就明确失败，让模型自己圆过去。"""
    from nonebot.adapters.onebot.v11 import MessageSegment

    started = time.perf_counter()
    cfg = getattr(context, "config", None)
    if cfg is not None and not bool(getattr(cfg, "self_image_enable", True)):
        return ToolResult(
            tool_name="self_image", tool_call_id=call.id, success=False,
            error="自画像功能关着", error_type="disabled",
        )

    hint = str(call.arguments.get("hint") or "").strip()
    path = pick_portrait(cfg, hint)

    if path is not None:
        # 出站前先归一化：素材可能是几千像素的原图，直接发既慢又可能被
        # QQ 压成一团。压到 1280 长边、1.5MB 以内，肉眼看不出差别。
        from .media_normalize import normalize_file

        try:
            # 压出来的副本放到 cache 子目录，不落在素材目录里——
            # 否则素材池会被自己的产物越塞越多。
            path = normalize_file(path, out_dir=portrait_dir(cfg) / "cache")
        except Exception:  # noqa: BLE001 — 压不了就发原图
            pass
        log_event("self_image.send", source="dir", name=path.name)
        return ToolResult(
            tool_name="self_image", tool_call_id=call.id, success=True,
            output="发了",
            data={"value": MessageSegment.image(path.as_uri())},
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    raw = resolve_portrait_bytes(cfg)
    if raw:
        from .media_normalize import normalize_image_bytes

        try:
            raw = normalize_image_bytes(raw)
        except Exception:  # noqa: BLE001
            pass
        log_event("self_image.send", source="library", bytes=len(raw))
        return ToolResult(
            tool_name="self_image", tool_call_id=call.id, success=True,
            output="发了",
            data={"value": MessageSegment.image(raw)},
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    # 没有素材不是「故障」，是「还没准备」。给模型的措辞要能让它自然带过，
    # 而不是逼它去编一张不存在的照片。
    return ToolResult(
        tool_name="self_image", tool_call_id=call.id, success=False,
        error="你手头没有自己的图（素材目录是空的），别编，直接岔开说别的",
        error_type="no_assets",
        duration_ms=(time.perf_counter() - started) * 1000,
    )


def _visible_self_image(ctx: Any) -> bool:
    cfg = getattr(ctx, "config", None)
    if cfg is None:
        return False
    return bool(getattr(cfg, "self_image_enable", True))


def build_self_image_tool() -> ToolDefinition:
    return ToolDefinition(
        name="self_image",
        description="发一张自己的图（被问长相 / 要照片时用）",
        marker=PORTRAIT_MARKER,
        prompt_text=PORTRAIT_PROMPT_TEXT,
        parser=parse_portrait_request,
        executor=execute_self_image,
        failure_policy=FailurePolicy.FALLBACK_TEXT,
        continuation=Continuation.SIDE_EFFECT_ONLY,
        permission="social.self_image",
        visibility=_visible_self_image,
        owner="social_layer",
        priority=53,
        timeout=20.0,
        parameter_schema={
            "type": "object",
            "properties": {
                "hint": {"type": "string", "description": "可选关键词，如 全身 / 头像"},
            },
        },
    )


def register_self_image_tool(registry: Any) -> None:
    registry.register(build_self_image_tool())


__all__ = [
    "PORTRAIT_MARKER",
    "PORTRAIT_PROMPT_TEXT",
    "build_self_image_tool",
    "execute_self_image",
    "list_portraits",
    "parse_portrait_request",
    "pick_portrait",
    "portrait_dir",
    "register_self_image_tool",
    "resolve_portrait_bytes",
]
