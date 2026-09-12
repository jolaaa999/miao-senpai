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
_DRAW_INTENT_RE = re.compile(
    r"(画|绘)(一?[张个幅只])?(图|画|插画|海报|头像|壁纸|封面)?"
    r"|(生成|做|来)(一?[张个幅])?(图|画|插画|海报|头像|壁纸|封面)"
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


class DrawCooldown:
    def __init__(self) -> None:
        self._last: dict[str, float] = {}

    def ready(self, session_id: str, cooldown_sec: int, now: float | None = None) -> bool:
        if cooldown_sec <= 0:
            return True
        if session_id not in self._last:
            return True
        now = time.time() if now is None else now
        return (now - self._last[session_id]) >= cooldown_sec

    def mark(self, session_id: str, now: float | None = None) -> None:
        self._last[session_id] = time.time() if now is None else now


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


def _cache_key(config: PluginConfig, prompt: str) -> str:
    return (
        f"{config.draw_api_key_resolved()[:8]}|"
        f"{config.draw_base_url_resolved()}|"
        f"{config.draw_model_resolved()}|"
        f"{config.draw_size}|{config.draw_quality}|{prompt}"
    )


def _cache_path(config: PluginConfig, prompt: str) -> Path:
    digest = hashlib.sha256(_cache_key(config, prompt).encode()).hexdigest()[:20]
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


async def generate_image_file(
    prompt: str,
    config: PluginConfig | None = None,
) -> Path | None:
    cfg = config or get_config()
    prompt = (prompt or "").strip()
    if not prompt or not cfg.draw_configured():
        return None

    target = _cache_path(cfg, prompt)
    if target.is_file() and target.stat().st_size > 0:
        return target

    client = _draw_client(cfg)
    model = cfg.draw_model_resolved()
    size = cfg.draw_size.strip() or "1024x1024"
    quality = cfg.draw_quality.strip()
    timeout = max(30.0, float(cfg.draw_timeout))
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
            item = resp.data[0] if resp.data else None
            if item is None:
                raise RuntimeError("empty image response")

            saved = False
            if getattr(item, "b64_json", None):
                saved = await _save_b64_image(str(item.b64_json), tmp)
            elif getattr(item, "url", None):
                saved = await _download_image(str(item.url), tmp, timeout=timeout)

            if not saved:
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
) -> MessageSegment | None:
    cfg = config or get_config()
    if not cfg.draw_enable or interrupt:
        return None
    prompt = (resolved_prompt or "").strip()
    if not prompt:
        prompt = resolve_draw_prompt(
            marker_prompt=marker_prompt,
            user_request=user_request,
            reply_text=reply_text,
            max_chars=cfg.draw_max_prompt_chars,
        )
    if not prompt:
        return None
    logger.info(f"dl_senpai draw final prompt={prompt[:180]!r}")
    path = await generate_image_file(prompt, cfg)
    if path is None:
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
    return MessageSegment.image(file=send_path.resolve().as_uri())
