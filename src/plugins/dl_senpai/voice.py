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

# GPT-SoVITS v2/v2Pro 认可的 text_lang 取值。写错（比如 "jp"）api_v2 会直接 400，
# 语音功能就静默失效了，所以锁值先在这一层过滤一遍。
_TTS_SUPPORTED_LANGS = frozenset({
    "auto",
    "auto_yue",
    "en",
    "zh",
    "ja",
    "yue",
    "ko",
    "all_zh",
    "all_ja",
    "all_yue",
    "all_ko",
})

# 「只会说日语」的锁值可能写成 ja 或 all_ja，两者都是「全句按日语读」。
_JA_LOCK_VALUES = frozenset({"ja", "all_ja"})


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


def lang_lock(cfg: PluginConfig) -> str:
    """返回生效的语音语言锁（规范化语言码），未锁或值非法时返回空串。

    非法值当没锁：宁可按原来的自动判断，也不要让一个拼错的键把语音整条废掉。
    """
    raw = str(getattr(cfg, "voice_lang_lock", "") or "").strip().lower()
    return raw if raw in _TTS_SUPPORTED_LANGS else ""


def _is_japanese_text(text: str, *, min_kana: int = 3) -> bool:
    """够不够「日文」：至少出现 min_kana 个假名。全汉字的日文句子（罕见）会被判否，
    这是有意的保守策略——宁可多翻一次，也不要把中文汉字当成日文念出去。"""
    return len(_JA_KANA_RE.findall(text or "")) >= min_kana


_JA_TRANSLATE_SYSTEM = (
    "你是中日翻译引擎，不是聊天助手，不要回答内容本身。"
    "任务：把用户给的中文句子翻成自然的口语日语（动漫女高中生口吻），保留原来的情绪"
    "（撒娇、得意、安慰、吐槽等）和大致长度。"
    "输出规则：只输出日语译文本身，不能出现任何中文句子、解释、引号或罗马字。"
)

# 模型偶尔会「聊天式」地回中文（实测 deepseek-v4.1-flash 会），所以失败要纠正重试一次：
# 第二次把上面的中文输出当负例贴回去，比单纯重复一遍有效。
_JA_RETRY_HINT = (
    "你上一次输出的还是中文，不合格。这次只准输出日语（可以夹少量日文汉字），"
    "像「おやすみ、今日もお疲れさま。ゆっくり休んでね」这样。要翻译的原文：{text}"
)


async def to_japanese(text: str, cfg: PluginConfig) -> str | None:
    """把要念的句子换成日语；失败或结果不像日语时返回 None。

    为什么非得先换语言：GPT-SoVITS 的 ja 前端只会按日语音素读，直接喂中文汉字出来的
    是汉字音读/乱读，不是「她在说日语」。所以锁定日语时，念的内容必须真的先变成日语。
    失败一律 None —— 调用方据此跳过这条语音，绝不退化成中文语音。
    """
    from .llm import get_llm  # 延迟导入：llm 模块重，且避免模块级循环依赖

    try:
        llm = get_llm(cfg)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai voice: llm unavailable for translate err={exc}")
        return None

    last = ""
    for attempt in range(2):
        prompt = text if attempt == 0 else _JA_RETRY_HINT.format(text=text)
        try:
            resp = await llm.client.chat.completions.create(
                model=cfg.active_model(),
                messages=[
                    {"role": "system", "content": _JA_TRANSLATE_SYSTEM},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.3,
                max_tokens=300,
                timeout=20,
            )
            out = (resp.choices[0].message.content or "").strip()
        except Exception as exc:  # noqa: BLE001 — 翻译失败不该拦掉整条回复
            logger.warning(
                f"dl_senpai voice: japanese translate failed err={type(exc).__name__}: {exc}"
            )
            return None
        out = re.sub(r"^[\"'「『]+|[\"'」』]+$", "", out).strip()
        if _is_japanese_text(out):
            return out
        last = out[:80]
    logger.warning(
        f"dl_senpai voice: translated text is not japanese, skip voice sample={last!r}"
    )
    return None

def resolve_gptsovits_text_lang(spoken: str, user_request: str, cfg: PluginConfig) -> str:
    lock = lang_lock(cfg)
    if lock:
        # 锁定时连「用户当场要求说英语」也不破例：这把锁表达的是她「只会说这门语言」。
        return lock
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
        # 锁定语言时不回退 edge：那套音色（zh-CN-XiaoyiNeural）念出来的既不是她的声线，
        # 也不会是日语 —— 破锁比「这条没语音」更糟。
        if cfg.gptsovits_fallback_edge and not lang_lock(cfg):
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
    # 锁定日语时先把要念的内容换成日语（模型偶尔仍会写中文）；换不出来就这条不发，
    # 绝不发中文语音——「只会说日语」是设定，不是建议。
    if lang_lock(cfg) in _JA_LOCK_VALUES and not _is_japanese_text(spoken):
        translated = await to_japanese(spoken, cfg)
        if not translated:
            return None
        spoken = text_for_voice(translated, max_chars=cfg.voice_max_chars) or translated
        if cfg.resolved_voice_tts_provider() == "gptsovits":
            text_lang = "ja"
    path = await synthesize_voice_file(spoken, cfg, text_lang=text_lang)
    if path is None:
        return None
    return MessageSegment.record(file=path.resolve().as_uri())
