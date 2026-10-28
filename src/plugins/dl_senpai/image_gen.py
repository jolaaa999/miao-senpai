from __future__ import annotations

import base64
import hashlib
import uuid
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageSegment
from openai import APIError, APIStatusError, AsyncOpenAI, RateLimitError

from .cache import SessionCooldown
from .config import PluginConfig, get_config

SenpaiDrawKind = Literal["general", "appearance", "outfit"]

_DRAW_MARKER_RE = re.compile(
    r"<<<DRAW\s*(.+?)\s*(?:>>>|>>|＞＞＞|】】|】>|】|＞＞)"
    r"[。．.!！?？～~\s]*",
    re.IGNORECASE | re.DOTALL,
)
_DRAW_MARKER_FALLBACK_RE = re.compile(
    r"<<<DRAW[\s\S]*?(?:>>>|>>|＞＞＞|】】|】>|】|＞＞|$)",
    re.IGNORECASE,
)
# 第二支原本要求「生成/做/来」+ 量词 + 名词**紧邻**，于是「生成一张橘猫图片」
# 这种把主体插在量词和名词之间的说法匹配不上——恰恰是最自然的问法，漏判之后
# 兜底重试（recovery）也不会触发，等于白丢一轮。改法：带量词时允许中间夹一段
# 极短的主体（≤6 个非标点字符）；不带量词时仍要求名词紧跟，以免把「做这个任务
# 要查图」之类的闲聊误判成生图请求。
_DRAW_INTENT_RE = re.compile(
    r"(画|绘)(一?[张个幅只])?(图|画|插画|海报|头像|壁纸|封面)?"
    r"|(生成|做|来)(?:一?[张个幅])[^\s，。！？、,.!?]{0,6}(图|画|插画|海报|头像|壁纸|封面)"
    r"|(生成|做|来)(图|画|插画|海报|头像|壁纸|封面)"
    r"|生图|画图|作画|出图|文生图",
    re.IGNORECASE,
)
_APPEARANCE_INTENT_RE = re.compile(
    r"长什么样|啥样|什么样子|什么模样|外貌|长相|颜值|形象|模样"
    r"|发(一?张|个)?(图|照片|自拍)|看看(你|学姐|长什么样)"
    r"|你(的)?(照片|自拍|本人|样子)|自拍|本人照|爆照|发个照",
    re.IGNORECASE,
)
_OUTFIT_INTENT_RE = re.compile(
    r"换(一?身|件|个)?衣服|给你换|帮你换|穿上|换上|试穿|给你穿|帮你穿|穿(一?身|件|个)"
    r"|学姐穿|想看.{0,12}穿|穿.{0,16}(洛丽塔|汉服|旗袍|水手服|女仆|泳装|jk|JK|连衣裙|制服)"
    r"|打扮(一下)?|试(试|穿).{0,10}(裙|衣|装|服|风)"
    r"|(jk|JK|洛丽塔|汉服|旗袍|水手服|女仆|泳装|睡衣|连衣裙|制服).{0,16}(穿|试试|换上|看看)",
    re.IGNORECASE,
)
_DRAW_NEGATE_RE = re.compile(
    r"(别|不要|不想|不用).{0,8}(画|图|生图|作画|照片|自拍|换衣服)"
    r"|(wiki|百科|别瞎编|搜索再回答|查清楚|怪物猎人)",
    re.IGNORECASE,
)
_DRAW_TRIGGER_CLEAN_RE = re.compile(
    r"[@＠]\S+|学姐|senpai|帮我|给我|请|能不能|可以|来(一?张|个)?|"
    r"(画|绘|生成|做)(一?张|个|幅)?(图|画|插画|海报|头像|壁纸|封面)?|"
    r"生图|画图|作画|出图|文生图",
    re.IGNORECASE,
)
_APPEARANCE_TRIGGER_CLEAN_RE = re.compile(
    r"[@＠]\S+|学姐|senpai|帮我|给我|请|能不能|可以|看看|看一下|"
    r"长什么样|啥样|什么样子|什么模样|外貌|长相|颜值|形象|模样|"
    r"发(一?张|个)?(图|照片|自拍)?|照片|自拍|本人照|爆照|发个照",
    re.IGNORECASE,
)
_OUTFIT_TRIGGER_CLEAN_RE = re.compile(
    r"[@＠]\S+|学姐|senpai|帮我|给我|请|能不能|可以|"
    r"换(一?身|件|个)?衣服|穿上|换上|试穿|给你穿|帮你穿|穿(一?身|件|个)?|"
    r"打扮(一下)?|试(试|穿)|看看",
    re.IGNORECASE,
)
_OUTFIT_EXTRACT_RE = re.compile(
    r"(?:换|穿|换上|试穿|打扮)(?:一身|一件|一套|个)?[：:\s]*(.+)"
    r"|(?:给你|帮你)(?:换|穿).{0,8}[：:\s]*(.+)"
    r"|(.{2,48}(?:裙|衣|袍|服|制服|外套|穿搭|汉服|jk|JK|洛丽塔|旗袍|水手服|女仆装|泳装))",
    re.IGNORECASE,
)
_ANGELINA_FACE = (
    "face reference only: Arknights Angelina inspired, long soft pink hair, "
    "fox ears, fluffy fox tail, warm brown eyes, youthful gentle anime face"
)
# 自设仅指面部/发型/兽耳等形象特征，不含游戏默认服装
_APPEARANCE_DEFAULT_PROMPT = (
    f"{_ANGELINA_FACE}, upper body portrait, gentle smile, "
    "simple plain casual top, not game default costume, soft pastel background"
)
_OUTFIT_PHRASE_MAP: tuple[tuple[str, str], ...] = (
    (
        "绿色洛丽塔",
        "vivid emerald green lolita dress, entire dress bright green fabric, "
        "green lace green ribbons green petticoat, no red clothing",
    ),
    ("白色洛丽塔", "elegant white lolita dress with lace, ribbons and petticoat"),
    ("黑色洛丽塔", "elegant black lolita dress with lace, ribbons and petticoat"),
    ("粉色洛丽塔", "elegant pink lolita dress with lace, ribbons and petticoat"),
    ("蓝色洛丽塔", "elegant blue lolita dress with lace ribbons frills and petticoat"),
    ("洛丽塔", "lolita dress with lace, ribbons, frills and petticoat"),
    ("绿色连衣裙", "elegant green dress"),
    ("白色连衣裙", "elegant white dress"),
    ("黑色连衣裙", "elegant black dress"),
    ("汉服", "traditional chinese hanfu dress"),
    ("旗袍", "chinese qipao cheongsam dress"),
    ("jk制服", "japanese sailor school uniform"),
    ("JK制服", "japanese sailor school uniform"),
    ("水手服", "japanese sailor school uniform"),
    ("女仆装", "maid dress with apron and headband"),
    ("泳装", "swimsuit"),
)
_COLOR_MAP: dict[str, str] = {
    "绿色": "green",
    "红色": "red",
    "白色": "white",
    "黑色": "black",
    "蓝色": "blue",
    "粉色": "pink",
    "紫色": "purple",
    "黄色": "yellow",
}
_DEFAULT_OUTFIT_NOISE_RE = re.compile(
    r"default\s+.+?(?:jacket|outfit|costume|uniform)|"
    r"messenger\s+.+?|tactical\s+.+?|red\s+jacket|game\s+costume|"
    r"angelina\s+inspired\s+.+?wearing\s+",
    re.IGNORECASE,
)
_TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class DrawRequest:
    text: str
    prompt: str = ""
    force: bool = False


class DrawCooldown(SessionCooldown):
    """生图冷却。Phase 2 统一到 `SessionCooldown`（原先为无界裸 dict）。"""


def parse_draw_request(raw_reply: str) -> DrawRequest:
    text = raw_reply or ""
    prompt = ""
    had_marker = False
    for match in _DRAW_MARKER_RE.finditer(text):
        had_marker = True
        part = (match.group(1) or "").strip()
        if part:
            prompt = part
    cleaned = _DRAW_MARKER_RE.sub("", text)
    cleaned = _DRAW_MARKER_FALLBACK_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return DrawRequest(text=cleaned, prompt=prompt, force=had_marker)


def strip_draw_markers(text: str) -> str:
    cleaned = _DRAW_MARKER_RE.sub("", text or "")
    cleaned = _DRAW_MARKER_FALLBACK_RE.sub("", cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def looks_like_draw_request(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if _DRAW_NEGATE_RE.search(raw):
        return False
    return _DRAW_INTENT_RE.search(raw) is not None


def looks_like_appearance_request(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if _DRAW_NEGATE_RE.search(raw):
        return False
    return _APPEARANCE_INTENT_RE.search(raw) is not None


def looks_like_outfit_request(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if _DRAW_NEGATE_RE.search(raw):
        return False
    return _OUTFIT_INTENT_RE.search(raw) is not None


def wants_senpai_image(text: str) -> bool:
    return (
        looks_like_draw_request(text)
        or looks_like_appearance_request(text)
        or looks_like_outfit_request(text)
    )


def classify_senpai_draw(user_request: str) -> SenpaiDrawKind:
    if looks_like_outfit_request(user_request):
        return "outfit"
    if looks_like_appearance_request(user_request):
        return "appearance"
    return "general"


def extract_draw_prompt_from_user(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    cleaned = _DRAW_TRIGGER_CLEAN_RE.sub(" ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，,。.!！?？~～")
    return cleaned


def extract_outfit_description(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    direct = re.search(r"穿(.+?)(?:的)?样子", raw)
    if direct:
        desc = _OUTFIT_TRIGGER_CLEAN_RE.sub(" ", direct.group(1))
        desc = re.sub(r"\s+", " ", desc).strip(" ，,。.!！?？~～")
        if len(desc) >= 2:
            return desc
    for match in _OUTFIT_EXTRACT_RE.finditer(raw):
        for group in match.groups():
            if not group:
                continue
            desc = _OUTFIT_TRIGGER_CLEAN_RE.sub(" ", group)
            desc = re.sub(r"\s+", " ", desc).strip(" ，,。.!！?？~～")
            if len(desc) >= 2:
                return desc
    cleaned = _OUTFIT_TRIGGER_CLEAN_RE.sub(" ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，,。.!！?？~～")
    return cleaned


def normalize_outfit_for_prompt(clothing: str) -> str:
    raw = re.sub(
        r"(的)?样子$|试试$|看看$|好不好$|更精致的版本$",
        "",
        (clothing or "").strip(),
    ).strip(" ，,。.!！?？~～")
    if not raw:
        return ""
    if not re.search(r"[\u4e00-\u9fff]", raw):
        return raw
    for phrase, english in _OUTFIT_PHRASE_MAP:
        if phrase in raw:
            return english
    colors: list[str] = []
    remainder = raw
    for cn, en in _COLOR_MAP.items():
        if cn in remainder:
            colors.append(en)
            remainder = remainder.replace(cn, "")
    remainder = remainder.strip()
    for phrase, english in _OUTFIT_PHRASE_MAP:
        if phrase in remainder or remainder in phrase:
            if colors:
                return f"{' '.join(colors)} {english}"
            return english
    if colors:
        return f"{' '.join(colors)} outfit"
    return raw


def _clothing_from_draw_marker(marker: str) -> str:
    text = (marker or "").strip()
    if not text:
        return ""
    for pattern in (
        r"wearing\s+an?\s+(.+?)(?:,|\.|$)",
        r"wearing\s+(.+?)(?:,|\.|$)",
        r"穿(?:着)?(.+)",
    ):
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            clothing = _DEFAULT_OUTFIT_NOISE_RE.sub("", match.group(1)).strip()
            clothing = re.sub(r"\s+", " ", clothing).strip(" ，,。.!！?？~～")
            if len(clothing) >= 2:
                return clothing
    return ""


def _outfit_color_constraints(outfit_en: str) -> str:
    lower = outfit_en.lower()
    if "green" in lower:
        return (
            "dominant color is vivid green, dress is entirely green, "
            "green fabric green lace green ribbons, absolutely no red clothing"
        )
    if "white" in lower:
        return "dominant color is white, no red jacket"
    if "black" in lower:
        return "dominant color is black, no red jacket"
    return ""


def resolve_outfit_clothing(*, marker_prompt: str, user_request: str) -> str:
    user = normalize_outfit_for_prompt(extract_outfit_description(user_request))
    if user:
        return user
    marker = normalize_outfit_for_prompt(_clothing_from_draw_marker(marker_prompt))
    if marker:
        return marker
    return ""


def extract_appearance_hint_from_user(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    cleaned = _APPEARANCE_TRIGGER_CLEAN_RE.sub(" ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，,。.!！?？~～")
    return cleaned


def _truncate_prompt(prompt: str, max_chars: int) -> str:
    prompt = re.sub(r"\s+", " ", (prompt or "").strip())
    if not prompt:
        return ""
    limit = max(16, max_chars)
    if len(prompt) <= limit:
        return prompt
    # 只留开头，不做保头保尾的「智能截断」。
    # 试过按 60/40 保头保尾，结果把中段的服装约束（"dominant color is vivid green,
    # dress is entirely green"）切掉了，测试立刻抓到——中段也可能有硬约束。
    # 真正该做的是**让它别撞上这个上限**：draw_max_prompt_chars 已从 800 提到 2000，
    # 实测完整提示词 1016~1485 字符、带问题重试约 1635，都在上限内，不会走到这里。
    # 这里只作为最后一道闸，保开头（角色身份锚定永远在最前面）。
    return prompt[: limit - 1].rstrip() + "…"


def build_angelina_appearance_prompt(*, extra: str = "") -> str:
    extra = (extra or "").strip()
    if extra and not _APPEARANCE_INTENT_RE.search(extra):
        return (
            f"{_ANGELINA_FACE}, {extra}, upper body portrait, "
            "simple plain casual clothes, not game default costume, soft background"
        )
    return _APPEARANCE_DEFAULT_PROMPT


def build_angelina_outfit_prompt(clothing: str) -> str:
    outfit = normalize_outfit_for_prompt(clothing) or (clothing or "").strip()
    color_hint = _outfit_color_constraints(outfit)
    parts = [
        "Anime full body fashion illustration.",
        f"CLOTHING ONLY: {outfit}.",
        f"The girl wears {outfit}. The outfit must exactly be {outfit}.",
    ]
    if color_hint:
        parts.append(color_hint)
    if "lolita" in outfit.lower():
        parts.append(
            "lolita dress with frills lace petticoat and bow details, not a jacket."
        )
    parts.extend(
        [
            f"{_ANGELINA_FACE}.",
            "Character reference is face hair ears and tail only, not clothing.",
            "NOT Arknights default costume, NOT red messenger jacket, NOT game outfit, no bag.",
            "outfit clearly visible, soft lighting, clean background",
        ]
    )
    return " ".join(parts)


def _normalize_raw_prompt(
    *,
    marker_prompt: str,
    user_request: str,
    reply_text: str,
) -> str:
    for candidate in (marker_prompt, extract_draw_prompt_from_user(user_request), reply_text):
        prompt = (candidate or "").strip()
        if not prompt:
            continue
        prompt = re.sub(r"<<<[^>]+>>>", "", prompt)
        prompt = re.sub(r"\s+", " ", prompt).strip()
        if prompt:
            return prompt
    return ""


def resolve_draw_prompt(
    *,
    marker_prompt: str,
    user_request: str,
    reply_text: str,
    max_chars: int,
    group_id: int | str | None = None,
) -> str:
    scene = classify_senpai_draw(user_request)
    raw = _normalize_raw_prompt(
        marker_prompt=marker_prompt,
        user_request=user_request,
        reply_text=reply_text,
    )

    if scene == "appearance":
        if group_id is not None:
            try:
                from .shop import group_outfit_en

                shop_outfit = group_outfit_en(group_id)
                if shop_outfit:
                    return _truncate_prompt(
                        build_angelina_outfit_prompt(shop_outfit),
                        max_chars,
                    )
            except Exception:  # noqa: BLE001
                pass
        extra = raw or extract_appearance_hint_from_user(user_request)
        if extra and not _APPEARANCE_INTENT_RE.search(extra):
            prompt = build_angelina_appearance_prompt(extra=extra)
        else:
            prompt = build_angelina_appearance_prompt()
        return _truncate_prompt(prompt, max_chars)

    if scene == "outfit":
        clothing = resolve_outfit_clothing(
            marker_prompt=marker_prompt,
            user_request=user_request,
        )
        if clothing:
            return _truncate_prompt(build_angelina_outfit_prompt(clothing), max_chars)
        return ""

    if raw:
        return _truncate_prompt(raw, max_chars)
    return ""


def draw_retry_hint(user_request: str) -> str:
    scene = classify_senpai_draw(user_request)
    if scene == "appearance":
        return (
            "对方想看学姐长什么样。你的外貌自设是明日方舟安洁莉娜：粉长发、狐耳狐尾、元气信使少女。"
            "请简短回应一两句，并在末尾补上指令行。"
            "格式：<<<DRAW Angelina inspired portrait, pink hair, fox ears, gentle smile>>>"
        )
    if scene == "outfit":
        clothing_en = normalize_outfit_for_prompt(extract_outfit_description(user_request))
        return (
            "对方要给你换衣服。安洁莉娜只是脸/发型/狐耳参考，不要画游戏默认衣服。"
            "<<<DRAW>>> 里只写对方描述的衣服（含颜色），不要写 red jacket / messenger / default outfit。"
            f"格式：<<<DRAW wearing {clothing_en or 'the outfit they described'}, full body>>>"
        )
    return (
        "对方在要求你画图。请简短回应一两句，并在末尾补上指令行。"
        "描述要具体：主体、风格、氛围。"
        "格式：<<<DRAW 画面描述>>>"
    )


def draw_failure_note() -> str:
    return "图没画出来……学姐这笔今天有点卡，你换个描述再试一次？"


def draw_block_note(reason: str, *, scene: SenpaiDrawKind = "general") -> str:
    if reason == "cooldown":
        return "画画也要歇一会儿啦，过会儿再找我出图～"
    if reason == "empty_prompt":
        if scene == "outfit":
            return "你想给我换什么衣服呀？比如「白色连衣裙」「JK制服」「汉服风」～"
        if scene == "appearance":
            return "嗯……照片等学姐收拾一下再发你～"
        return "你想画啥得说清楚一点呀，比如「画一只趴在键盘上的橘猫」。"
    return ""


def _status_code(exc: BaseException) -> int | None:
    code = getattr(exc, "status_code", None)
    return int(code) if isinstance(code, int) else None


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, RateLimitError):
        return True
    code = _status_code(exc)
    return code in _TRANSIENT_STATUS if code is not None else False


def _cache_key(config: PluginConfig, prompt: str, extra: str = "") -> str:
    # extra 放参考图指纹：同一句提示词换了参考图，必须算出不同的键，
    # 否则会直接命中上一次（没参考图）的缓存图，看起来像「改图没生效」。
    return (
        f"{config.draw_api_key_resolved()[:8]}|"
        f"{config.draw_base_url_resolved()}|"
        f"{config.draw_model_resolved()}|"
        f"{config.draw_size}|{config.draw_quality}|{extra}|{prompt}"
    )


def _cache_path(config: PluginConfig, prompt: str, extra: str = "") -> Path:
    digest = hashlib.sha256(_cache_key(config, prompt, extra).encode()).hexdigest()[:20]
    root = Path(config.draw_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{digest}.png"


def _draw_client(config: PluginConfig) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=config.draw_api_key_resolved(),
        base_url=config.draw_base_url_resolved(),
        timeout=max(30.0, float(config.draw_timeout)),
    )


async def _download_image(url: str, target: Path, *, timeout: float) -> bool:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.content
        if not data:
            return False
        target.write_bytes(data)
        return target.is_file() and target.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai draw download failed url={url[:72]} err={exc}")
        return False


async def _save_b64_image(b64_data: str, target: Path) -> bool:
    try:
        raw = base64.b64decode(b64_data)
        if not raw:
            return False
        target.write_bytes(raw)
        return target.is_file() and target.stat().st_size > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai draw b64 decode failed: {exc}")
        return False


_REFERENCE_INTENT_RE = re.compile(
    r"(按|照|依|用|参考|根据|基于|以)[^。！？\n]{0,8}(这张|这个|这|上面那张|上图|原图|参考图)"
    r"|[这此]张(?:图|图片|照片|插画|立绘)|参考图|原图"
    r"|保持[^。！？\n]{0,6}构图"
)


def _sniff_image_ext(raw: bytes) -> str:
    """按文件头判断真实格式。

    为什么不用固定 .png：QQ 的图多数是 jpeg/webp，却常被标成 png 的 data URL。
    images.edit 会按文件名推断 MIME，名实不符时上游直接拒掉。
    """
    if raw.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if raw.startswith(b"\x89PNG"):
        return ".png"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return ".png"


async def _load_reference_image(
    sources: list[str],
    *,
    timeout: float,
) -> tuple[str, bytes] | None:
    """取本轮用户发的第一张可用图片，返回 (文件名, 原始字节)。

    为什么只放内存：参考图是一次性的，落盘还要管清理和并发覆盖，没必要。
    任何一张取不到都跳过，全失败返回 None——参考图只是增强，不能因为它
    取不到就把整次生图拖垮（调用方会退化成纯文生图）。
    """
    for src in sources or []:
        text = str(src or "").strip()
        if not text:
            continue
        raw = b""
        try:
            if text.startswith("data:"):
                _, _, b64 = text.partition(",")
                if not b64:
                    continue
                raw = base64.b64decode(b64)
            elif text.startswith(("http://", "https://")):
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
                    resp = await client.get(text)
                    resp.raise_for_status()
                    raw = resp.content
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"dl_senpai draw reference fetch failed err={type(exc).__name__}: {exc}"
            )
            continue
        if not raw:
            continue
        # 4MB 以上多半不是「参考图」而是误传的大图，硬塞给编辑接口只会超时
        if len(raw) > 4 * 1024 * 1024:
            logger.warning(f"dl_senpai draw reference too large bytes={len(raw)}")
            continue
        return (f"reference{_sniff_image_ext(raw)}", raw)
    return None


async def _persist_response_item(
    resp: object,
    tmp: Path,
    *,
    timeout: float,
) -> bool:
    """把一次模型响应里的图片写到 tmp，成功返回 True。"""
    data = getattr(resp, "data", None) or []
    item = data[0] if data else None
    if item is None:
        raise RuntimeError("empty image response")
    if getattr(item, "b64_json", None):
        return await _save_b64_image(str(item.b64_json), tmp)
    if getattr(item, "url", None):
        return await _download_image(str(item.url), tmp, timeout=timeout)
    return False


async def generate_image_file(
    prompt: str,
    config: PluginConfig | None = None,
    *,
    reference: tuple[str, bytes] | None = None,
    fresh: bool = False,
) -> Path | None:
    """出图。给了 reference 就先走图生图（images.edit），失败退化成纯文生图。

    为什么失败要退化而不是直接报错：图生图是上游按「分组」开通的能力，
    没开通时返回 403（线上实测过）。参考图属于增强项，不能因为它不可用
    就让用户一张图都拿不到。
    """
    cfg = config or get_config()
    prompt = (prompt or "").strip()
    if not prompt or not cfg.draw_configured():
        return None

    ref_fp = hashlib.sha256(reference[1]).hexdigest()[:16] if reference else ""
    target = _cache_path(cfg, prompt, extra=ref_fp)
    # 缓存键只由「配置 + 参考图指纹 + 提示词」组成，**没有时间维度**。
    # 对生图这种「每次都要一张新图」的语义，永久缓存是错的：同一句话画过一次，
    # 之后每次再画都会拿回同一张。2026-10-28 实测：7 个不同 id 的归档文件
    # md5 完全相同（854df67ebfcd），全部是同一个缓存文件的副本，
    # 用户看到的就是「怎么画都是这张」。
    # 所以生图主链路一律传 fresh=True；只有确实想要复用（批量补图）才不传。
    if fresh:
        logger.info(
            f"dl_senpai draw fresh key={target.stem} prompt_len={len(prompt)}"
        )
    elif target.is_file() and target.stat().st_size > 0:
        logger.info(
            f"dl_senpai draw cache_hit key={target.stem} bytes={target.stat().st_size} "
            f"prompt_len={len(prompt)}"
        )
        return target

    client = _draw_client(cfg)
    model = cfg.draw_model_resolved()
    size = cfg.draw_size.strip() or "1024x1024"
    quality = cfg.draw_quality.strip()
    timeout = max(30.0, float(cfg.draw_timeout))
    # tmp 带随机后缀：fresh 模式下同一句提示词可能有多个任务并发算到同一个 target，
    # 共用一个 tmp 文件会互相覆盖，最后谁先 replace 谁赢——那张图可能根本不属于这个任务。
    tmp = target.with_suffix(f".{uuid.uuid4().hex[:6]}.tmp.png")

    # 先试图生图：一次就够，不跟文生图挤在同一个重试循环里，否则日志分不清
    # 到底是哪条路失败
    if reference is not None:
        try:
            # size 必须显式传：不传时上游会自己挑尺寸（实测 1254x1254），
            # 和文生图那条路的画幅对不上，同一句提示词出来的构图会跳
            resp = await client.images.edit(
                model=model, image=reference, prompt=prompt, n=1, size=size,
            )
            if await _persist_response_item(resp, tmp, timeout=timeout):
                tmp.replace(target)
                logger.info(
                    f"dl_senpai draw ok (reference) model={model} "
                    f"prompt_len={len(prompt)} bytes={target.stat().st_size}"
                )
                return target
            logger.warning("dl_senpai draw reference returned empty payload")
        except (APIStatusError, APIError) as exc:
            logger.warning(
                f"dl_senpai draw edit unavailable status={_status_code(exc)} "
                f"fallback=generate err={str(exc)[:160]}"
            )
            # 退回无参考图的缓存位，避免把文生图结果写进「参考图」的缓存键
            target = _cache_path(cfg, prompt)
            tmp = target.with_suffix(".tmp.png")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"dl_senpai draw edit failed fallback=generate "
                f"err={type(exc).__name__}: {str(exc)[:160]}"
            )
            target = _cache_path(cfg, prompt)
            tmp = target.with_suffix(".tmp.png")

    last_error: Exception | None = None
    for attempt in range(3):
        try:
            kwargs: dict[str, object] = {
                "model": model,
                "prompt": prompt,
                "n": 1,
            }
            if cfg.draw_uses_dalle_params():
                kwargs["size"] = size
                if quality:
                    kwargs["quality"] = quality
            elif size:
                kwargs["size"] = size
            resp = await client.images.generate(**kwargs)
            if not await _persist_response_item(resp, tmp, timeout=timeout):
                raise RuntimeError("image payload empty")

            tmp.replace(target)
            logger.info(
                f"dl_senpai draw ok model={model} size={size} "
                f"prompt_len={len(prompt)} bytes={target.stat().st_size}"
            )
            return target
        except (RateLimitError, APIStatusError, APIError) as exc:
            last_error = exc
            logger.warning(
                f"dl_senpai draw api error model={model} "
                f"status={_status_code(exc)} attempt={attempt + 1}/3: {exc}"
            )
            if _is_transient(exc) and attempt < 2:
                continue
            break
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning(
                f"dl_senpai draw failed model={model} "
                f"attempt={attempt + 1}/3: {type(exc).__name__}: {exc}"
            )
            if attempt < 2:
                continue
            break

    if tmp.is_file():
        tmp.unlink(missing_ok=True)
    if last_error is not None:
        logger.warning(f"dl_senpai draw give up: {last_error}")
    return None


def _web_reference_mode(cfg: Any) -> str:
    """网图参考策略：off / verify_only / enabled。

    默认 verify_only —— 实测证据摆在那里：同人图 + images.edit 出来的成品
    被视觉模型判为「棕发精灵耳、野生二创」，比纯文本档案还差。所以网图默认
    只用来校验档案、记录来源，不许直接进图生图。
    """
    raw = str(getattr(cfg, "character_web_reference_mode", "verify_only") or "").strip().lower()
    return raw if raw in ("off", "verify_only", "enabled") else "verify_only"


async def build_image_segment(
    *,
    marker_prompt: str = "",
    user_request: str = "",
    reply_text: str = "",
    resolved_prompt: str = "",
    config: PluginConfig | None = None,
    interrupt: bool = False,
    archive_session_id: str = "",
    archive_source_user: str = "",
    archive_source_user_id: str = "",
    archive_scene: str = "",
    reference_images: list[str] | None = None,
    auto_reference: bool = False,
    reference_policy: str = "default",
) -> MessageSegment | None:
    """生成一张图并返回可发送的消息段。

    reference_policy 只给基准测试用，用来控制变量：
      default    现行为（用户图 > 本地核验库 > 网图按策略）
      text_only  完全不使用任何自动参考图（A 组）
      local_only 只用本地核验库，不联网搜图（B 组）
    """
    cfg = config or get_config()
    if not cfg.draw_enable or interrupt:
        return None

    # 一次生图一条 trace：出图「不像」时要能一眼看出断在哪个环节
    # （没识别出角色 / 参考图没加载 / 提示词被改写丢了身份 / 模型漂移）。
    from .image_trace import new_trace, write_debug_dump

    trace = new_trace(cfg, original_prompt=user_request or marker_prompt or resolved_prompt)

    prompt = (resolved_prompt or "").strip()
    if not prompt:
        prompt = resolve_draw_prompt(
            marker_prompt=marker_prompt,
            user_request=user_request,
            reply_text=reply_text,
            max_chars=cfg.draw_max_prompt_chars,
        )
    if not prompt:
        trace.finish("skipped", error="empty_prompt").log()
        return None
    # 只在本轮确实带图、且对方的话里明确指向那张图时才当参考图用。
    # 不加这层判断的话，顺手发张图再聊别的也会被误当成「按图改」。
    reference: tuple[str, bytes] | None = None
    if reference_images and _REFERENCE_INTENT_RE.search(user_request or ""):
        reference = await _load_reference_image(
            list(reference_images),
            timeout=max(30.0, float(cfg.draw_timeout)),
        )
        if reference is not None:
            trace.reference_mode = "user_image"
            trace.reference_count = 1
            logger.info(f"dl_senpai draw reference attached bytes={len(reference[1])}")

    # ---- 角色身份识别（已知 IP 角色） ----
    # 这是「画一张艾雅法拉」与「画一只猫」的分岔口：认出来走身份管线，
    # 认不出来一个字都不加，普通生图链路原样保留。
    from .character_resolver import resolve_character
    from .entity_prompt import UniversalEntityPromptBuilder
    from .image_intent import analyze_image_intent
    from .image_prompt_builder import ImagePromptBuilder

    resolution = resolve_character(user_request, draft_prompt=prompt, config=cfg)

    # ---- 通用意图识别 ----
    # 档案命中只是四条路里的一条。过去档案没命中就直接掉进普通生图，
    # 于是「画犬夜叉」变成一张泛泛的古风少年。这里补上「动态解析实体」，
    # 但成本纪律不变：命中缓存 0 次调用，未命中最多 1 次轻量解析。
    intent = await analyze_image_intent(
        user_request,
        draft_prompt=prompt,
        has_user_image=bool(reference_images),
        config=cfg,
    )
    trace.image_intent = intent.type
    trace.entity_count = len(intent.entities)
    trace.entity_names = tuple(e.canonical_name or e.name for e in intent.entities)
    trace.entity_source = intent.entity_source
    trace.entity_confidence = max((e.confidence for e in intent.entities), default=0.0)
    trace.entity_cache_hit = intent.entity_cache_hit
    trace.explicit_overrides = intent.override_map()
    if resolution.is_known and resolution.profile is not None:
        trace.intent = "known_character_generation"
        trace.character = resolution.profile.canonical_name
        trace.franchise = resolution.profile.franchise
        trace.character_confidence = resolution.confidence
        trace.character_reason = resolution.reason
        # 参考图优先级：用户图 > 本地**已核验**库 > 网图（按策略）。
        # 为什么本地库要求 manifest 里 verified=true：来源不明的图等于用一个未知角色
        # 锚定身份，实测正是它把艾雅法拉画成了棕发精灵耳。
        want_local = reference_policy in ("default", "local_only")
        if reference is None and want_local:
            from .character_references import resolve_library_reference

            # diagnostics 收起来：文件被换、hash 不符、清单里没核验过，
            # 是三种完全不同的问题，只有把原因带进 trace 才查得动。
            library_diag: list[str] = []
            library_ref = resolve_library_reference(
                cfg, resolution.profile, user_request=user_request, diagnostics=library_diag
            )
            if library_diag:
                trace.extra["reference_integrity_failed"] = library_diag[:5]
            if library_ref is not None:
                reference = library_ref.as_pair()
                trace.reference_mode = "local_verified"
                trace.reference_count = 1
                # 只有人工核验过的图才算 verified；assistant_reviewed 会给 False，
                # 于是 Level B（"以参考图为准"）的提示词分支根本不会触发。
                trace.reference_verified = library_ref.verified
                trace.reference_verification_status = library_ref.verification_status.value
                trace.reference_source = "character_library"
                trace.reference_source_trust = library_ref.source_trust
                trace.extra["reference_priority"] = library_ref.priority
                trace.extra["reference_file"] = library_ref.filename
    else:
        trace.character_reason = resolution.reason

    # 网图：普通图沿用原行为（找一张当参考）；已知角色按 web_reference_mode 决定用途。
    web_mode = _web_reference_mode(cfg) if resolution.is_known else "enabled"
    # local_only / text_only 是基准测试用来控制变量的：这两种模式下**不许**联网，
    # 否则「只用本地库」那组会偷偷被网图补上，A/B 就失去意义了。
    # 通用生图默认 text-first：不联网。
    # 旧行为是「普通图也找一张网图当参考」，那意味着「画一只猫」要多花几秒搜索、
    # 还可能被一张不相关的图带偏。搜图现在只服务已知实体的身份佐证，
    # 普通图要搜必须显式打开 draw_web_reference_for_general。
    want_web = reference_policy == "default" and (
        intent.type == "known_entity_generation"
        or bool(getattr(cfg, "draw_web_reference_for_general", False))
    )
    # ---- 参考图流水线：先凑 3 张，交叉核对后只送 1 张锚定 ----
    # 为什么值得多花十几秒：单张参考图错了就只能错到底，没有任何东西能验证它。
    # 三张独立搜来的图可以互相印证——两张以上都出现的特征才写进提示词，
    # 再由视觉模型指出哪张最像，而不是盲拿 URL 顺序第一张（经常是缩略图）。
    # 这套跑失败时下面原来的单张网搜仍然是兜底，不会让用户一张图都拿不到。
    consensus_traits: tuple[str, ...] = ();
    _ref_conflict = False
    if (
        reference is None
        and auto_reference
        and want_web
        and cfg.draw_find_reference
        and web_mode != "off"
        # 这个开关本身就是授权：开了就允许网图锚点进 images.edit（下面是安全阀）
        and bool(getattr(cfg, "draw_reference_pipeline", False))
    ):
        from .draw_pipeline import (
            analyze_references,
            collect_references,
            pick_anchor,
            traits_block,
        )

        _profile = resolution.profile if resolution.is_known else None
        # 把档案里的设定原样交给核对步骤，让它判断「网图跟设定对不对得上」。
        # 这一步不额外花钱：跟提取共识特征是同一个请求。
        _expect = ""
        if _profile is not None:
            try:
                _tiers = _profile.identity_tiers()
                _expect = "\n".join(
                    f"- {_profile.visual_identity.label(k)}：{v}"
                    for k, v in (
                        list(_tiers["human"]) + list(_tiers["reviewed"]) + list(_tiers["unverified"])
                    )
                )
            except Exception:  # noqa: BLE001 — 拼不出来就不判一致性
                _expect = ""
        _subject = (
            _profile.canonical_name if _profile is not None
            else (intent.entities[0].name if intent.entities else "")
        )
        try:
            _cands = await collect_references(
                cfg, profile=_profile, user_request=user_request,
                subject=_subject, user_pair=reference,
            )
            _analysis = await analyze_references(
                cfg, _cands, subject=_subject, expectations=_expect
            )
            _anchor = pick_anchor(_cands, _analysis)
        except Exception as exc:  # noqa: BLE001 — 流水线是增强项，失败就退回单张逻辑
            logger.warning(
                f"dl_senpai draw pipeline failed fallback=single "
                f"err={type(exc).__name__}: {str(exc)[:120]}"
            )
            _cands, _analysis, _anchor = [], None, None
        if _cands:
            trace.extra["reference_candidates"] = [c.as_dict() for c in _cands]
        if _analysis is not None:
            trace.extra["reference_analysis"] = _analysis.as_dict()
            consensus_traits = _analysis.traits
        # 安全阀：至少两张候选、且视觉核对成功，才允许网图锚点进 images.edit。
        # 单张网图正是当初被实测否掉的那条路，不能借流水线的名义绕回来。
        # 2026-10-27 实测：搜到的三张全是棕发二创，它们互相"共识"一致，
        # 于是 traits 把棕发写进提示词、edit 又跟着锚点走，画出来全是棕发，
        # 白烧一张 4 分钟的图之后才被自检发现。所以一致性和共识缺一不可。
        _anchor_ok = (
            _anchor is not None
            and _anchor.source != "user_image"
            and len(_cands) >= 2
            and _analysis is not None
            and _analysis.ok
            and _analysis.consistent
        )
        if _analysis is not None and not _analysis.consistent:
            trace.extra["reference_conflict"] = list(_analysis.differences)
            logger.warning(
                f"dl_senpai draw pipeline references contradict profile "
                f"diffs={list(_analysis.differences)[:2]} fallback=text_only"
            )
            # 冲突的图连"共同特征"也不能信，一并丢掉
            consensus_traits = ()
            # 已经确认网图跟设定不符，就别再走下面的单张网搜兜底了——
            # 那只会在同一个搜索引擎上再找一张同样错的图，白花 40 秒再被拒一次。
            _ref_conflict = True
            # 记下来，7 天内不再为这个角色白搜一遍（网图源短期内不会变好）
            try:
                from .draw_pipeline import mark_references_unusable

                mark_references_unusable(
                    _subject, differences=_analysis.differences
                )
            except Exception:  # noqa: BLE001 — 记账失败不影响出图
                pass
        if not _anchor_ok and _anchor is not None and _anchor.source != "user_image":
            logger.info(
                f"dl_senpai draw pipeline anchor_rejected refs={len(_cands)} "
                f"analysis_ok={bool(_analysis and _analysis.ok)}"
            )
        if _anchor_ok:
            reference = (_anchor.name, _anchor.raw)
            trace.reference_mode = (
                "local_verified" if _anchor.source == "local_verified" else "web_verified"
            )
            trace.reference_count = 1
            trace.reference_source = _anchor.source
            trace.extra["reference_anchor_sha256"] = _anchor.sha256[:16]
            logger.info(
                f"dl_senpai draw pipeline refs={len(_cands)} anchor={_anchor.source} "
                f"traits={len(consensus_traits)}"
            )

    # 网图黑名单：这个角色的图源刚被判过「跟设定不符」，7 天内不再联网找。
    # 只拦「联网找图」，不拦用户自己发的图——用户给的图永远优先。
    _web_subject = ""
    try:
        _web_subject = (
            resolution.profile.canonical_name
            if resolution.is_known and resolution.profile is not None
            else (intent.entities[0].name if intent.entities else "")
        )
    except Exception:  # noqa: BLE001 — 推不出角色名就当没命中
        _web_subject = ""
    _ref_blocked = False
    if _web_subject:
        try:
            from .draw_pipeline import references_known_unusable

            _ref_blocked = references_known_unusable(_web_subject)
        except Exception:  # noqa: BLE001 — 黑名单坏了不能挡出图
            _ref_blocked = False
    if _ref_blocked:
        logger.info(
            f"dl_senpai draw web search skipped subject={_web_subject!r} reason=known_unusable"
        )
    if (
        reference is None
        and not _ref_conflict
        and not _ref_blocked
        and auto_reference
        and want_web
        and cfg.draw_find_reference
        and web_mode != "off"
    ):
        from .draw_reference import find_reference_image

        found = await find_reference_image(
            cfg,
            prompt=prompt,
            user_request=user_request,
            # 认得出来就带上规范英文名，搜索召回明显更好（艾雅法拉 → Eyjafjalla）
            character=(
                resolution.profile.canonical_name
                if resolution.is_known and resolution.profile is not None
                else ""
            ),
            # 已知角色要来源画像（域名/类型/可信度），普通图不需要
            with_meta=bool(resolution.is_known),
        )
        web_meta = found if hasattr(found, "as_pair") else None
        web_pair = found.as_pair() if web_meta is not None else found

        if web_pair is not None and resolution.is_known and resolution.profile is not None:
            # 已知角色的网图：先验设定，再按策略决定能不能进 images.edit
            ok, issue, score = True, "", None
            if bool(getattr(cfg, "character_verify_reference", True)):
                from .character_references import verify_reference

                ok, issue, score = await verify_reference(cfg, resolution.profile, web_pair[1])
            trust = float(getattr(web_meta, "source_trust", 0.0) or 0.0) if web_meta is not None else 0.0
            source_type = getattr(web_meta, "source_type", "unknown") if web_meta is not None else "unknown"
            source_domain = getattr(web_meta, "source_domain", "") if web_meta is not None else ""
            trace.reference_source = f"web:{source_domain or 'unknown'}"
            trace.reference_source_trust = trust
            trace.extra["web_reference"] = {
                "source_type": source_type,
                "source_domain": source_domain,
                "source_trust": round(trust, 2),
                "visual_score": score,
                "issue": (issue or "")[:80],
            }
            min_trust = float(getattr(cfg, "character_web_reference_min_trust", 0.7) or 0.7)
            if web_mode == "enabled" and ok and trust >= min_trust:
                reference = web_pair
                trace.reference_mode = "web_verified"
                trace.reference_count = 1
                trace.reference_verified = False
            else:
                # verify_only（默认）或未达标：只留证据，绝不送进图生图
                trace.reference_mode = "web_probe_only" if ok else "web_rejected"
                trace.reference_count = 0
                if not ok:
                    trace.extra["reference_issue"] = (issue or "设定不一致")[:120]
        elif web_pair is not None:
            # 普通图：维持原行为（有参考图就用）
            reference = web_pair
            if trace.reference_mode == "none":
                trace.reference_mode = "web_search"
                trace.reference_count = 1

    card = None
    identity_requirements = ""

    if resolution.is_known and resolution.profile is not None:
        # 已知角色：身份段落完全由档案决定，**不走 enhance**。
        # 为什么跳过扩写：那一步是让另一个模型重写提示词，实测它会把专有名词
        # 同义化（艾雅法拉 → 羊角少女、凯尔希 → 精灵耳医生），正是身份管线要防的事。
        # 代价是画风/光影少了点润色，但身份一致性优先——这是本次改造的核心取舍。
        # 只有「人工核验过的本地参考图」才配在提示词里说"以图为准"（Level B）。
        # 网图即使通过视觉校验也只是 verify_only/probe，不升级为身份权威；
        # assistant_reviewed 的库图同样不算（verified 派生属性为 False）。
        built = ImagePromptBuilder().build(
            user_request=user_request,
            draft_prompt=prompt,
            resolution=resolution,
            reference_verified=(
                reference is not None
                and trace.reference_mode == "local_verified"
                and bool(trace.reference_verified)
            ),
        )
        # 已知角色提示词更长（身份条款 + 场景），上限单独放宽，
        # 否则身份写全了、场景会被尾部截断，出现「场景丢失」的新问题。
        prompt = _truncate_prompt(
            built.text, max(int(cfg.draw_max_prompt_chars), 1800)
        )
        identity_requirements = built.identity_block
        if built.negatives:
            identity_requirements += "\n禁止：" + "；".join(built.negatives)
        trace.extra["enhance_skipped"] = "known_character"
        trace.extra["identity_priority"] = built.identity_priority
        # 把「这段提示词里的哪些约束有依据」记下来：以后怀疑画错时，
        # 第一个要查的就是这次到底是硬约束还是软提示。
        trace.character_profile_status = built.profile_status
        trace.human_verified_fields = built.human_verified_fields
        trace.assistant_reviewed_fields = built.assistant_reviewed_fields
        trace.unverified_fields = built.unverified_fields
    elif intent.is_known_entity:
        # 动态实体：本地没有档案，但有名字（可能还有少量高置信特征）。
        # 同样跳过 enhance —— 理由和上面一样，扩写会做同义化，专有名词首当其冲。
        built = UniversalEntityPromptBuilder().build(
            user_request=user_request,
            intent=intent,
            draft_prompt=prompt,
        )
        prompt = _truncate_prompt(built.text, max(int(cfg.draw_max_prompt_chars), 1800))
        identity_requirements = built.identity_block
        if built.negatives:
            identity_requirements += "\n禁止：" + "；".join(built.negatives)
        trace.extra["enhance_skipped"] = "known_entity"
        trace.extra["identity_priority"] = built.identity_priority
        trace.character_profile_status = built.profile_status
        # 没有人工核验字段：这些都是模型侧摘要，绝不能记成硬约束
        trace.human_verified_fields = ()
        trace.assistant_reviewed_fields = built.assistant_reviewed_fields
        trace.unverified_fields = ()
        trace.character = built.character
        trace.franchise = built.franchise
        trace.character_confidence = intent.confidence
        trace.character_reason = intent.reason

    else:
        # 普通图：维持原有链路，行为与改动前完全一致（不许被角色管线污染）
        from .draw_cards import augment_prompt, match_character_card

        card = match_character_card(prompt, user_request)

        # 提示词增强：本中转的生图模型不做任何改写（实测 revised_prompt 原样返回），
        # 一句短描述直接进模型，等于让模型自己去猜构图和画风，这里把这一层补上。
        if cfg.draw_enhance:
            from .draw_enhance import enhance_prompt

            prompt = await enhance_prompt(
                cfg,
                prompt=prompt,
                user_request=user_request,
                reference=reference,
                card_spec=card.spec if card is not None else "",
                locked_entities=tuple(e.canonical_name or e.name for e in intent.entities),
            )

        # 官方设定仍然要压在最前面（截断之前拼，否则被截掉就等于没加）
        prompt = _truncate_prompt(
            augment_prompt(prompt, user_request), cfg.draw_max_prompt_chars
        )
        identity_requirements = card.spec if card is not None else ""

    # 三张参考图的共同特征：介于「人工核验硬约束」和「单图随意描述」之间的软提示。
    # 放在身份段之后、场景段之前，且明确标注不是硬规则，避免又变成编造。
    _traits_text = ""
    if consensus_traits:
        from .draw_pipeline import traits_block

        _tb = traits_block(consensus_traits)
        if _tb:
            _traits_text = _tb
            prompt = _truncate_prompt(
                f"{prompt}\n\n{_tb}", max(int(cfg.draw_max_prompt_chars), 2000)
            )

    trace.final_prompt = prompt
    trace.generation_mode = "edit" if reference is not None else "text_only"
    logger.info(f"dl_senpai draw final prompt={prompt[:180]!r}")

    # 生图主链路每次都要新图，不吃缓存
    path = await generate_image_file(prompt, cfg, reference=reference, fresh=True)

    # 出图自检：不合格就带着**具体问题**重画。
    # 为什么必须带问题：不告诉它错在哪，同一个模型会用同样的方式再错一遍。
    if path is not None and cfg.draw_verify and cfg.draw_max_retry > 0:
        from .draw_enhance import judge_image

        # 服装/场景/姿势以用户要求为准。2026-10-28 实测：用户要「穿死库水」，
        # 自检却因为「缺少黑色罗德岛制服和火山法杖」判不合格，白重画一张 60 秒。
        # 身体特征（发色瞳色耳朵角尾巴）仍必须符合设定——那部分不能让。
        requirements = (
            identity_requirements
            + "\n用户要求：" + (user_request or "")
            + "\n判定规则：服装、场景、姿势一律以「用户要求」为准；"
            + "用户明确指定了泳装/浴衣/便服/睡衣/校服等，缺少默认制服或标志性道具"
            + "不算错，也不要要求补上。但发色、瞳色、耳朵、角、尾巴等身体特征"
            + "仍必须符合设定。"
        ).strip()
        ok, issue = await judge_image(cfg, path, requirements=requirements)
        attempts = 0
        while not ok and attempts < cfg.draw_max_retry:
            attempts += 1
            # 身份类问题多半是参考图把设定带偏了。再拿同一张图重画只会得到
            # 同一张错图（实测：棕褐发色，重画仍棕褐，白烧 4 分钟又超时）。
            # 这类重试必须**换依据**：丢掉参考图和网图共识特征，只按档案画。
            _identity_issue = bool(re.search(
                r"发色|头发|瞳|眼睛|服装|服饰|衣|角|尾巴|配色|颜色|设定|身份|角色",
                issue or "",
            ))
            _drop_reference = _identity_issue and reference is not None
            _retry_ref = None if _drop_reference else reference
            _base_prompt = prompt
            if _identity_issue and _traits_text:
                _base_prompt = _base_prompt.replace(_traits_text, "")
            _hint = (
                "\n（上一次参考了不准确的网图，导致设定画错；这次不要参考图，严格按角色设定画）"
                if _drop_reference
                else ""
            )
            retry_prompt = _truncate_prompt(
                f"{_base_prompt}\n\n上一次生成的问题（这次必须修正）：{issue}{_hint}",
                cfg.draw_max_prompt_chars,
            )
            logger.info(
                f"dl_senpai draw retry {attempts} issue={issue[:80]!r} "
                f"drop_reference={_drop_reference}"
            )
            # 重画同理：命中旧缓存等于没重画
            retry_path = await generate_image_file(
                retry_prompt, cfg, reference=_retry_ref, fresh=True
            )
            if retry_path is None:
                break
            path, prompt = retry_path, retry_prompt
            ok, issue = await judge_image(cfg, path, requirements=requirements)
    # 身份评分：只测量记录，绝不触发重画（重画策略要单独设计，否则费用失控）
    if (
        path is not None
        and resolution.is_known
        and resolution.profile is not None
        and bool(getattr(cfg, "identity_eval", True))
    ):
        try:
            from .character_references import count_verified_references
            from .identity_eval import CharacterIdentityEvaluator

            evaluator = CharacterIdentityEvaluator(
                resolution.profile,
                verified_reference_count=count_verified_references(cfg, resolution.profile),
            )
            score = await evaluator.evaluate(cfg, path.read_bytes())
            trace.identity_score = score.overall
            trace.identity_dimensions = dict(score.dimensions)
            trace.identity_issues = score.issues
            trace.identity_verdict = score.verdict
            trace.extra["identity_basis"] = score.basis
        except Exception as exc:  # noqa: BLE001 — 评分失败不能让出图失败
            logger.debug(f"dl_senpai identity eval skipped: {type(exc).__name__}: {exc}")

    if path is None:
        trace.finish("no_image").log()
        write_debug_dump(cfg, trace)
        return None
    send_path = path
    if archive_session_id:
        from .draw_store import get_draw_store

        archived = get_draw_store(cfg).archive_from_cache(
            archive_session_id,
            cache_path=path,
            prompt=prompt,
            scene=archive_scene,
            user_request=user_request,
            source_user=archive_source_user,
            source_user_id=archive_source_user_id,
            model=cfg.draw_model_resolved(),
            size=cfg.draw_size.strip() or "1024x1024",
        )
        if archived is not None:
            send_path = archived
    trace.finish("success").log()
    write_debug_dump(cfg, trace)
    return MessageSegment.image(file=send_path.resolve().as_uri())
