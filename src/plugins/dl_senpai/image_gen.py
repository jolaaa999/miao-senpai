from __future__ import annotations

import base64
import hashlib
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
    if target.is_file() and target.stat().st_size > 0:
        return target

    client = _draw_client(cfg)
    model = cfg.draw_model_resolved()
    size = cfg.draw_size.strip() or "1024x1024"
    quality = cfg.draw_quality.strip()
    timeout = max(30.0, float(cfg.draw_timeout))
    tmp = target.with_suffix(".tmp.png")

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
) -> MessageSegment | None:
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
    from .image_prompt_builder import ImagePromptBuilder

    resolution = resolve_character(user_request, draft_prompt=prompt, config=cfg)
    if resolution.is_known and resolution.profile is not None:
        trace.intent = "known_character_generation"
        trace.character = resolution.profile.canonical_name
        trace.franchise = resolution.profile.franchise
        trace.character_confidence = resolution.confidence
        trace.character_reason = resolution.reason
        # 本地角色库优先于上网搜：那张图是你确认过的，网图可能同人/带水印
        if reference is None:
            from .character_references import find_library_reference

            library_ref = find_library_reference(cfg, resolution.profile)
            if library_ref is not None:
                reference = library_ref
                trace.reference_mode = "character_library"
                trace.reference_count = 1
    else:
        trace.character_reason = resolution.reason

    # 用户没给图（或没说要「按这张」）时，派子 agent 上网找一张。
    # 为什么要找：没有参考图时模型只能凭记忆编设定，实测把凯尔希的猫耳画成精灵耳。
    # 自拍/换装类在 find_reference_image 内部被排除（她的外观有固定 prompt）。
    if reference is None and auto_reference and cfg.draw_find_reference:
        from .draw_reference import find_reference_image

        reference = await find_reference_image(
            cfg,
            prompt=prompt,
            user_request=user_request,
            # 认得出来就带上规范英文名，搜索召回明显更好（艾雅法拉 → Eyjafjalla）
            character=(
                resolution.profile.canonical_name
                if resolution.is_known and resolution.profile is not None
                else ""
            ),
        )
        if reference is not None and trace.reference_mode == "none":
            trace.reference_mode = "web_search"
            trace.reference_count = 1

        # 网图当参考图之前先验设定。实测证据（2026-10-27）：同一个「画一张艾雅法拉」，
        # 纯文本那张被判定「可识别为艾雅法拉」，而用了 deviantart 同人图做图生图的那张
        # 变成「棕发 + 精灵耳，野生二创」——图生图会把参考图的错设定原样搬过来，
        # 比不给参考图更差。所以已知角色的网图必须过校验；本地库/用户图不校验。
        if (
            reference is not None
            and resolution.is_known
            and resolution.profile is not None
            and trace.reference_mode == "web_search"
            and bool(getattr(cfg, "character_verify_reference", True))
        ):
            from .character_references import verify_reference

            trusted, issue = await verify_reference(cfg, resolution.profile, reference[1])
            if not trusted:
                trace.reference_mode = "web_search_rejected"
                trace.reference_count = 0
                trace.extra["reference_issue"] = issue[:120] or "设定不一致"
                reference = None

    card = None
    identity_requirements = ""

    if resolution.is_known and resolution.profile is not None:
        # 已知角色：身份段落完全由档案决定，**不走 enhance**。
        # 为什么跳过扩写：那一步是让另一个模型重写提示词，实测它会把专有名词
        # 同义化（艾雅法拉 → 羊角少女、凯尔希 → 精灵耳医生），正是身份管线要防的事。
        # 代价是画风/光影少了点润色，但身份一致性优先——这是本次改造的核心取舍。
        built = ImagePromptBuilder().build(
            user_request=user_request,
            draft_prompt=prompt,
            resolution=resolution,
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
            )

        # 官方设定仍然要压在最前面（截断之前拼，否则被截掉就等于没加）
        prompt = _truncate_prompt(
            augment_prompt(prompt, user_request), cfg.draw_max_prompt_chars
        )
        identity_requirements = card.spec if card is not None else ""

    trace.final_prompt = prompt
    trace.generation_mode = "edit" if reference is not None else "text_only"
    logger.info(f"dl_senpai draw final prompt={prompt[:180]!r}")

    path = await generate_image_file(prompt, cfg, reference=reference)

    # 出图自检：不合格就带着**具体问题**重画。
    # 为什么必须带问题：不告诉它错在哪，同一个模型会用同样的方式再错一遍。
    if path is not None and cfg.draw_verify and cfg.draw_max_retry > 0:
        from .draw_enhance import judge_image

        requirements = (identity_requirements + "\n用户要求：" + (user_request or "")).strip()
        ok, issue = await judge_image(cfg, path, requirements=requirements)
        attempts = 0
        while not ok and attempts < cfg.draw_max_retry:
            attempts += 1
            retry_prompt = _truncate_prompt(
                f"{prompt}\n\n上一次生成的问题（这次必须修正）：{issue}",
                cfg.draw_max_prompt_chars,
            )
            logger.info(f"dl_senpai draw retry {attempts} issue={issue[:80]!r}")
            retry_path = await generate_image_file(retry_prompt, cfg, reference=reference)
            if retry_path is None:
                break
            path, prompt = retry_path, retry_prompt
            ok, issue = await judge_image(cfg, path, requirements=requirements)
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
