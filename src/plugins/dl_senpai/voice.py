from __future__ import annotations

import hashlib
import re
import wave
from dataclasses import dataclass
from pathlib import Path

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageSegment

from .config import PluginConfig, get_config

_VOICE_MARKER_RE = re.compile(
    r"<<<VOICE"
    r"(?:\s+"
    r"(?P<content>.+?)"
    r")?"
    r"\s*"
    r"(?:>>>|】】|】>|】|>>|＞＞＞)"
    r"[。．.!！?？～~\s]*",
    re.IGNORECASE,
)
_VOICE_MARKER_TRAILING_JUNK_RE = re.compile(r"[】】【]+[。．.!！?？～~\s]*$")
_VOICE_INTENT_RE = re.compile(
    r"(发|用|来)(个|一段)?语音|语音(说|回|念|读)|用声音(说|回)|念(一?遍|出来)"
)
_VOICE_NEGATE_RE = re.compile(r"(别|不要|不想|不用).{0,6}语音")
_VOICE_QUOTE_RE = re.compile(
    r"(?:以下内容|如下|这句[话話]?|念(?:一?下)?)[：:\s]*[「『“\"](.+?)[」』”\"]"
    r"|[「『“\"](.+?)[」』”\"]",
)
_VOICE_JA_HINT_RE = re.compile(r"日语|日文|日本語|にほんご", re.IGNORECASE)
_VOICE_EN_HINT_RE = re.compile(r"英语|英文|english", re.IGNORECASE)
_JA_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002700-\U000027BF"
    "\U00002600-\U000026FF"
    "\U0001F600-\U0001F64F"
    "]+",
    flags=re.UNICODE,
)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _ref_audio_duration_sec(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as handle:
            rate = handle.getframerate()
            if rate <= 0:
                return None
            return handle.getnframes() / float(rate)
    except Exception:  # noqa: BLE001
        return None


@dataclass(frozen=True)
class VoiceRequest:
    text: str
    force: bool
    override: str = ""


def parse_voice_request(raw_reply: str) -> VoiceRequest:
    text = raw_reply or ""
    override = ""
    had_marker = False
    for match in _VOICE_MARKER_RE.finditer(text):
        had_marker = True
        part = (match.group("content") or "").strip()
        if part:
            override = part
    cleaned = _VOICE_MARKER_RE.sub("", text)
    cleaned = _VOICE_MARKER_TRAILING_JUNK_RE.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return VoiceRequest(text=cleaned, force=had_marker, override=override)


def looks_like_voice_request(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    if _VOICE_NEGATE_RE.search(raw):
        return False
    return _VOICE_INTENT_RE.search(raw) is not None


def extract_voice_quote_from_user(text: str) -> str:
    raw = (text or "").strip()
    if not raw:
        return ""
    match = _VOICE_QUOTE_RE.search(raw)
    if not match:
        return ""
    return (match.group(1) or match.group(2) or "").strip()


def resolve_gptsovits_text_lang(spoken: str, user_request: str, cfg: PluginConfig) -> str:
    request = (user_request or "").strip()
    if _VOICE_JA_HINT_RE.search(request):
        return "ja"
    if _VOICE_EN_HINT_RE.search(request):
        return "en"
    kana_count = len(_JA_KANA_RE.findall(spoken or ""))
    if kana_count >= 3:
        return "ja"
    return cfg.gptsovits_text_lang.strip() or "zh"


def text_for_voice(reply: str, *, max_chars: int, override: str = "") -> str | None:
    source = (override or reply or "").strip()
    if not source:
        return None
    text = _EMOJI_RE.sub("", source)
    text = _URL_RE.sub("", text)
    text = re.sub(r"<<<[^>]+>>>", "", text)
    text = re.sub(r"[\*#`_~\[\]]+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" ，,。.!！?？~～")
    if not text:
        return None
    limit = max(20, max_chars)
    if len(text) > limit:
        cut = text[:limit].rstrip()
        for sep in ("。", "！", "？", "，", ",", " "):
            pos = cut.rfind(sep)
            if pos >= limit // 2:
                cut = cut[: pos + 1].rstrip()
                break
        text = cut.rstrip("，, ") + "。"
    return text or None


def _cache_key(config: PluginConfig, text: str, text_lang: str = "") -> str:
    if config.resolved_voice_tts_provider() == "gptsovits":
        ref = config.gptsovits_ref_audio_path()
        ref_part = str(ref) if ref else ""
        lang = text_lang or config.gptsovits_text_lang.strip() or "zh"
        return (
            f"gptsovits|{config.gptsovits_base_url}|{ref_part}|"
            f"{config.gptsovits_prompt_lang}|{config.gptsovits_prompt_text}|"
            f"{lang}|{config.gptsovits_speed_factor}|{text}"
        )
    voice = config.resolved_voice_settings()
    return f"edge|{voice.profile}|{voice.name}|{voice.rate}|{voice.pitch}|{text}"


def _cache_path(config: PluginConfig, text: str, text_lang: str = "") -> Path:
    digest = hashlib.sha256(_cache_key(config, text, text_lang).encode()).hexdigest()[:20]
    root = Path(config.voice_dir)
    root.mkdir(parents=True, exist_ok=True)
    ext = ".wav" if config.resolved_voice_tts_provider() == "gptsovits" else ".mp3"
    return root / f"{digest}{ext}"


async def _synthesize_edge_file(text: str, cfg: PluginConfig, target: Path) -> Path | None:
    try:
        import edge_tts
    except ImportError:
        logger.warning("dl_senpai voice: edge-tts not installed, run pip install edge-tts")
        return None

    voice = cfg.resolved_voice_settings()
    communicate = edge_tts.Communicate(
        text,
        voice=voice.name,
        rate=voice.rate,
        pitch=voice.pitch,
    )
    tmp = target.with_suffix(".tmp.mp3")
    try:
        await communicate.save(str(tmp))
        if not tmp.is_file() or tmp.stat().st_size == 0:
            return None
        tmp.replace(target)
        return target
    except Exception:  # noqa: BLE001
        logger.exception("dl_senpai edge-tts synthesis failed")
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
        return None


async def _synthesize_gptsovits_file(
    text: str,
    cfg: PluginConfig,
    target: Path,
    *,
    text_lang: str | None = None,
) -> Path | None:
    ref = cfg.gptsovits_ref_audio_path()
    if ref is None:
        logger.warning(
            "dl_senpai gptsovits: ref audio missing, set DL_SENPAI_GPTSOVITS_REF_AUDIO"
        )
        return None
    ref_duration = _ref_audio_duration_sec(ref)
    if ref_duration is None or ref_duration < 3 or ref_duration > 10:
        logger.warning(
            f"dl_senpai gptsovits: ref audio duration={ref_duration}s "
            f"(need 3~10s), path={ref}"
        )
        return None
    prompt = cfg.gptsovits_prompt_text.strip()
    if not prompt:
        logger.warning("dl_senpai gptsovits: prompt text empty")
        return None

    base = cfg.gptsovits_base_url.strip().rstrip("/")
    lang = (text_lang or cfg.gptsovits_text_lang.strip() or "zh").strip()
    payload = {
        "text": text,
        "text_lang": lang,
        "ref_audio_path": str(ref),
        "prompt_text": prompt,
        "prompt_lang": cfg.gptsovits_prompt_lang.strip() or "zh",
        "text_split_method": cfg.gptsovits_text_split_method.strip() or "cut5",
        "media_type": "wav",
        "streaming_mode": False,
        "speed_factor": float(cfg.gptsovits_speed_factor),
        "batch_size": 1,
    }
    tmp = target.with_suffix(".tmp.wav")
    try:
        async with httpx.AsyncClient(timeout=cfg.gptsovits_timeout) as client:
            resp = await client.post(f"{base}/tts", json=payload)
        if resp.status_code != 200:
            detail = resp.text[:300]
            logger.warning(
                f"dl_senpai gptsovits api error status={resp.status_code} body={detail}"
            )
            return None
        data = resp.content
        if not data:
            return None
        tmp.write_bytes(data)
        if tmp.stat().st_size == 0:
            return None
        tmp.replace(target)
        logger.info(
            f"dl_senpai gptsovits ok text_len={len(text)} lang={lang} bytes={target.stat().st_size}"
        )
        return target
    except httpx.ConnectError:
        logger.warning(
            f"dl_senpai gptsovits: cannot connect {base} — is api_v2.py running?"
        )
        return None
    except Exception:  # noqa: BLE001
        logger.exception("dl_senpai gptsovits synthesis failed")
        if tmp.is_file():
            tmp.unlink(missing_ok=True)
        return None


async def synthesize_voice_file(
    text: str,
    config: PluginConfig | None = None,
    *,
    text_lang: str | None = None,
) -> Path | None:
    cfg = config or get_config()
    lang = text_lang or cfg.gptsovits_text_lang.strip() or "zh"
    target = _cache_path(cfg, text, lang)
    if target.is_file() and target.stat().st_size > 0:
        return target

    if cfg.resolved_voice_tts_provider() == "gptsovits":
        path = await _synthesize_gptsovits_file(text, cfg, target, text_lang=lang)
        if path is not None:
            return path
        if cfg.gptsovits_fallback_edge:
            logger.info("dl_senpai gptsovits failed, fallback to edge-tts")
            edge_target = target.with_suffix(".mp3")
            return await _synthesize_edge_file(text, cfg, edge_target)
        logger.warning("dl_senpai gptsovits failed and edge fallback is disabled")
        return None

    return await _synthesize_edge_file(text, cfg, target)


async def build_voice_segment(
    reply_text: str,
    *,
    config: PluginConfig | None = None,
    force: bool = False,
    override: str = "",
    user_request: str = "",
    interrupt: bool = False,
) -> MessageSegment | None:
    cfg = config or get_config()
    if not cfg.voice_enable or interrupt:
        return None
    spoken = text_for_voice(
        reply_text,
        max_chars=cfg.voice_max_chars,
        override=override,
    )
    if not spoken:
        return None
    text_lang = None
    if cfg.resolved_voice_tts_provider() == "gptsovits":
        text_lang = resolve_gptsovits_text_lang(spoken, user_request, cfg)
    path = await synthesize_voice_file(spoken, cfg, text_lang=text_lang)
    if path is None:
        return None
    return MessageSegment.record(file=path.resolve().as_uri())
