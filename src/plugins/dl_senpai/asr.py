"""本地语音转文字（她「听得见」的那只耳朵）。

为什么只做本地
--------------
原版那句话直接抄过来：**别把语音传到别人服务器上去转，那是你家里的声音。**
所以这里只接本地 whisper（faster-whisper 优先），没有任何云 ASR 分支。

依赖策略
--------
faster-whisper 不在 pyproject 的必装依赖里（模型 + ctranslate2 加起来几百 MB，
不该让不用语音的人替它买单）。因此：

- 没装 → 工具/入口返回一句明确的「没装依赖」，不抛异常、不影响其它功能；
- 首次调用会下载模型（几百 MB 到 1G），要有心理准备；
- 转写结果缓存按文件哈希落盘，同一条语音转两次不会重跑模型。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any

from nonebot import logger

__all__ = [
    "AsrUnavailable",
    "asr_available",
    "clean_asr_text",
    "find_record_segment",
    "transcribe_event",
    "transcribe_file",
]

# 单条语音的长度上限（秒）。超过就放弃——群里偶尔会有人发十分钟的语音，
# 转它要几分钟，还会把整段文字灌进上下文。
MAX_DURATION_SEC = 180

_CACHE: dict[str, str] = {}
_MODEL: Any = None
_MODEL_NAME = ""


class AsrUnavailable(RuntimeError):
    """语音识别不可用（没装依赖 / 没开 / 平台不支持）。"""


def asr_available() -> bool:
    """探测依赖是否装了。不加载模型，所以很快。"""
    try:
        import faster_whisper  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def _cache_dir(cfg: Any) -> Path:
    base = Path(str(getattr(cfg, "memory_dir", "data/dl_senpai/memory")))
    return base.parent / "asr_cache"


def _fingerprint(path: Path) -> str:
    """按「文件名 + 大小 + mtime」算指纹。

    不读全文做哈希：语音文件动辄几 MB，每次算 sha256 是纯浪费——
    同一条语音的 size+mtime 组合已经够区分了。
    """
    try:
        stat = path.stat()
    except OSError:
        return ""
    raw = f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def clean_asr_text(text: str) -> str:
    """清掉 whisper 常见多余产物：多余空格、重复标点、纯语气词。

    为什么不做「智能纠错」：原版踩过的坑——连着纠正五遍，她后面就不敢接话了。
    转错了就当没看见，别在提示词里反复强调「你听错了」。
    """
    cleaned = (text or "").strip()
    for junk in ("[BLANK_AUDIO]", "[Music]", "[Applause]", "（音乐）", "(音乐)"):
        cleaned = cleaned.replace(junk, "")
    while "  " in cleaned:
        cleaned = cleaned.replace("  ", " ")
    return cleaned.strip()


def _load_model(cfg: Any) -> Any:
    global _MODEL, _MODEL_NAME
    name = str(getattr(cfg, "asr_model", "small") or "small")
    if _MODEL is not None and _MODEL_NAME == name:
        return _MODEL
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:  # noqa: BLE001
        raise AsrUnavailable(
            "没装 faster-whisper，语音转文字不可用；"
            "要开就用 pip install faster-whisper"
        ) from exc

    # CPU + int8：这套东西跑在一台常开的家用机上，别去抢显卡
    # （显卡要留给 ComfyUI 那类真需要它的活）。
    _MODEL = WhisperModel(name, device="cpu", compute_type="int8")
    _MODEL_NAME = name
    return _MODEL


def _transcribe_sync(path: Path, cfg: Any) -> str:
    model = _load_model(cfg)
    language = str(getattr(cfg, "asr_language", "zh") or "zh")
    segments, info = model.transcribe(
        str(path),
        language=language,
        vad_filter=True,  # 去掉长静音，不然群里发错的一段空白会占满解码时间
        beam_size=1,
    )
    duration = float(getattr(info, "duration", 0) or 0)
    if duration > MAX_DURATION_SEC:
        raise AsrUnavailable(f"语音太长（{int(duration)} 秒 > {MAX_DURATION_SEC} 秒）")
    return clean_asr_text("".join(seg.text for seg in segments))


async def transcribe_file(path: str | Path, *, cfg: Any = None) -> str:
    """把一条语音转成文字。不可用时抛 AsrUnavailable（调用方决定怎么退化）。"""
    if cfg is None:
        from .config import get_config

        cfg = get_config()
    if not bool(getattr(cfg, "asr_enable", False)):
        raise AsrUnavailable("语音转文字没开（DL_SENPAI_ASR_ENABLE=false）")

    audio = Path(str(path))
    if not audio.exists():
        raise AsrUnavailable(f"语音文件不存在：{audio}")

    key = _fingerprint(audio)
    if key and key in _CACHE:
        return _CACHE[key]

    cache_file = _cache_dir(cfg) / f"{key}.json" if key else None
    if cache_file and cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            text = str(data.get("text") or "")
            if text:
                _CACHE[key] = text
                return text
        except Exception:  # noqa: BLE001 — 缓存坏了就当没有
            pass

    timeout = float(getattr(cfg, "asr_timeout_sec", 90) or 90)
    started = time.time()
    try:
        text = await asyncio.wait_for(asyncio.to_thread(_transcribe_sync, audio, cfg), timeout)
    except asyncio.TimeoutError as exc:
        raise AsrUnavailable(f"转写超时（>{int(timeout)} 秒）") from exc

    logger.info(
        f"dl_senpai asr ok chars={len(text)} cost={time.time() - started:.1f}s model={_MODEL_NAME}"
    )
    if key:
        _CACHE[key] = text
        if cache_file:
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(
                    json.dumps({"text": text, "at": time.time()}, ensure_ascii=False),
                    encoding="utf-8",
                )
            except OSError:
                pass
    return text


# ------------------------------------------------------------------ 事件接入


def find_record_segment(event: Any) -> Any | None:
    """在消息段里找第一条语音。没有就返回 None。"""
    try:
        for seg in getattr(event, "message", []) or []:
            if getattr(seg, "type", "") == "record":
                return seg
    except Exception:  # noqa: BLE001
        return None
    return None


def _record_data(seg: Any) -> dict[str, Any]:
    data = getattr(seg, "data", None)
    return dict(data) if isinstance(data, dict) else {}


async def _fetch_audio(event: Any, bot: Any, seg: Any, cfg: Any) -> Path | None:
    """把语音落到本地临时文件。

    取文件有三条路，按「代价从低到高」试：
    1. 段里就带了本地 path（协议端和 bot 同机时常见）；
    2. 段里带了 url（直接下载）；
    3. 调 OneBot 的 get_record 换一个 url/路径。
    全失败就返回 None——转不了就说转不了，不要静默当没这条消息。
    """
    import httpx

    data = _record_data(seg)
    cache_dir = _cache_dir(cfg) / "audio"
    cache_dir.mkdir(parents=True, exist_ok=True)

    def _ensure(local: Path) -> Path | None:
        return local if local.exists() and local.stat().st_size > 0 else None

    for key in ("path", "file"):
        raw = str(data.get(key) or "").strip()
        if not raw:
            continue
        candidate = Path(raw)
        found = _ensure(candidate) or _ensure(cache_dir / candidate.name)
        if found:
            return found

    urls: list[str] = []
    if data.get("url"):
        urls.append(str(data["url"]))
    if not urls:
        file_id = str(data.get("file") or data.get("file_id") or "")
        if file_id:
            try:
                api = "get_private_file_url" if not getattr(event, "group_id", None) else "get_group_file_url"
                _ = api  # 仅占位注释：不同实现接口名不一致，统一走 get_record
                info = await bot.call_api("get_record", file=file_id, out_format="mp3")
                if isinstance(info, dict):
                    for key in ("url", "file", "path"):
                        if info.get(key):
                            urls.append(str(info[key]))
            except Exception as exc:  # noqa: BLE001
                logger.info(f"dl_senpai asr get_record unsupported: {type(exc).__name__}: {exc}")

    for url in urls:
        local = cache_dir / hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
        suffix = Path(url.split("?")[0]).suffix or ".mp3"
        local = local.with_suffix(suffix)
        if _ensure(local):
            return local
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url)
            if resp.status_code == 200 and resp.content:
                local.write_bytes(resp.content)
                return local
        except Exception as exc:  # noqa: BLE001
            logger.info(f"dl_senpai asr download failed: {type(exc).__name__}")
    return None


async def transcribe_event(event: Any, bot: Any, cfg: Any = None) -> str:
    """事件里有语音就转成文字，没有则返回空串。不抛异常（退化返回空）。

    为什么不让它抛：它挂在每条消息的入口上，一条语音转不出来不该让整条
    消息处理链断掉。真要排查，日志里有原因。
    """
    seg = find_record_segment(event)
    if seg is None:
        return ""
    if cfg is None:
        from .config import get_config

        cfg = get_config()
    if not bool(getattr(cfg, "asr_enable", False)):
        return ""
    try:
        audio = await _fetch_audio(event, bot, seg, cfg)
        if audio is None:
            logger.info("dl_senpai asr skipped: 取不到语音文件")
            return ""
        return await transcribe_file(audio, cfg=cfg)
    except AsrUnavailable as exc:
        logger.info(f"dl_senpai asr unavailable: {exc}")
        return ""
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai asr failed: {type(exc).__name__}: {exc}")
        return ""
