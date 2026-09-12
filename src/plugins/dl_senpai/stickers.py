from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlparse

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, MessageEvent, MessageSegment

from .config import PluginConfig, get_config
from .session_fs import session_id_to_fs_key

_STICKER_IMAGE_SUB_TYPES = {1, "1", 2, "2", "sticker", "emoji"}
_STICKER_SUMMARY_HINTS = ("[动画表情]", "[表情]", "emoji", "sticker")
_TOKEN_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]{2,}")
_STICKER_MARKER_RE = re.compile(
    r"<<<STICKER\s*([^>\n]+?)>>>",
    re.IGNORECASE,
)
_FAKE_EMOJI_RE = re.compile(
    r"[\[（(【]\s*[^\]）)】]{0,24}?(emoji|表情|微笑|温柔|开心|哭|炸毛)\s*[^\]）)】]{0,12}?[\]）)】]",
    re.IGNORECASE,
)


@dataclass
class StickerRecord:
    """一条可复用的表情包记录（相当于学姐自己的「收藏表情」）。"""

    id: str
    segment_type: str
    segment_data: dict[str, Any]
    context_text: str = ""
    keywords: list[str] = field(default_factory=list)
    use_count: int = 0
    collected_at: str = ""
    source_user: str = ""
    local_path: str = ""

    def to_segment(self) -> MessageSegment:
        if self.local_path and Path(self.local_path).is_file():
            # NapCat / go-cqhttp 对本地文件最稳
            path = Path(self.local_path).resolve()
            return MessageSegment.image(file=path.as_uri())
        data = dict(self.segment_data)
        return MessageSegment(self.segment_type, data)


@dataclass
class StickerRequest:
    text: str
    query: str = ""
    force: bool = False


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _is_sticker_image(data: dict[str, Any]) -> bool:
    sub_type = data.get("sub_type")
    if sub_type in _STICKER_IMAGE_SUB_TYPES:
        return True
    summary = str(data.get("summary", "")).lower()
    return any(hint.lower() in summary for hint in _STICKER_SUMMARY_HINTS)


def extract_sticker_segments(message: Any) -> list[tuple[str, dict[str, Any]]]:
    """从 OneBot Message 中提取可收藏的表情段。

    QQ 系统黄豆/小黄脸（face）刻意忽略：不收集、不计数、不触发表情跟进。
    """
    found: list[tuple[str, dict[str, Any]]] = []
    for seg in message:
        seg_type = getattr(seg, "type", None)
        seg_data = dict(getattr(seg, "data", {}) or {})
        if seg_type == "face":
            continue
        elif seg_type == "mface":
            found.append(("mface", seg_data))
        elif seg_type == "image" and _is_sticker_image(seg_data):
            found.append(("image", seg_data))
    return found


def _dedupe_key(segment_type: str, segment_data: dict[str, Any]) -> str:
    if segment_type == "face":
        raw = f"face:{segment_data.get('id', '')}"
    elif segment_type == "mface":
        raw = "|".join(
            str(segment_data.get(k, ""))
            for k in ("emoji_id", "emoji_package_id", "key", "url")
        )
        raw = f"mface:{raw}"
    else:
        raw = (
            f"image:"
            f"{segment_data.get('file') or segment_data.get('url') or segment_data.get('summary', '')}"
        )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def extract_keywords(*texts: str) -> list[str]:
    tokens: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for token in _TOKEN_RE.findall(text.lower()):
            if token in seen:
                continue
            seen.add(token)
            tokens.append(token)
    return tokens


def parse_sticker_request(raw_reply: str) -> StickerRequest:
    """剥掉 <<<STICKER ...>>>，并判断是否强制发表情。"""
    text = raw_reply or ""
    queries: list[str] = []
    for match in _STICKER_MARKER_RE.finditer(text):
        q = (match.group(1) or "").strip()
        if q:
            queries.append(q)
    cleaned = _STICKER_MARKER_RE.sub("", text)
    cleaned = _FAKE_EMOJI_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    query = " ".join(queries).strip()
    return StickerRequest(text=cleaned, query=query, force=bool(query))


def score_sticker(record: StickerRecord, query_terms: list[str]) -> float:
    type_bonus = {"image": 0.35, "mface": 0.3, "face": 0.0}.get(record.segment_type, 0.0)
    local_bonus = 0.2 if record.local_path and Path(record.local_path).is_file() else 0.0
    if not query_terms:
        return type_bonus + local_bonus
    haystack = " ".join([record.context_text, *record.keywords]).lower()
    hits = 0.0
    for term in query_terms:
        if term in haystack:
            hits += 1.0
            continue
        for kw in record.keywords:
            kw_lower = kw.lower()
            if kw_lower in term or term in kw_lower:
                hits += 0.8
                break
    if hits == 0:
        return type_bonus * 0.2 + local_bonus * 0.2
    reuse_penalty = min(record.use_count, 10) * 0.05
    return hits + type_bonus + local_bonus - reuse_penalty


def _guess_ext(url: str, content_type: str) -> str:
    path = urlparse(url).path.lower()
    for ext in (".gif", ".png", ".webp", ".jpg", ".jpeg"):
        if path.endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    ct = (content_type or "").lower()
    if "gif" in ct:
        return ".gif"
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    return ".jpg"


async def _download_bytes(url: str) -> tuple[bytes, str] | None:
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content, resp.headers.get("content-type", "")
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"dl_senpai sticker download failed: {exc}")
        return None


async def _fetch_via_get_image(bot: Bot, file_ref: str) -> bytes | None:
    try:
        resp = await bot.get_image(file=file_ref)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"dl_senpai sticker get_image failed: {exc}")
        return None
    if not isinstance(resp, dict):
        return None
    b64 = resp.get("base64") or resp.get("data")
    if isinstance(b64, str) and b64:
        import base64

        raw = b64.split(",", 1)[-1] if b64.startswith("data:") else b64
        try:
            return base64.b64decode(raw)
        except Exception:  # noqa: BLE001
            return None
    local = resp.get("file") or resp.get("path")
    if isinstance(local, str) and local and Path(local).is_file():
        return Path(local).read_bytes()
    return None


async def materialize_sticker_file(
    bot: Bot | None,
    *,
    sticker_id: str,
    segment_type: str,
    segment_data: dict[str, Any],
    files_dir: Path,
) -> str:
    """尽量把表情落到本地，便于之后稳定复用（学姐自己的收藏夹）。"""
    if segment_type == "face":
        return ""
    files_dir.mkdir(parents=True, exist_ok=True)
    existing = list(files_dir.glob(f"{sticker_id}.*"))
    if existing:
        return str(existing[0].resolve())

    url = str(segment_data.get("url") or "")
    file_ref = str(segment_data.get("file") or "")
    content: bytes | None = None
    content_type = ""

    if url.startswith(("http://", "https://")):
        got = await _download_bytes(url)
        if got:
            content, content_type = got

    if content is None and bot is not None and file_ref:
        content = await _fetch_via_get_image(bot, file_ref)

    if not content:
        return ""

    ext = _guess_ext(url or file_ref, content_type)
    path = files_dir / f"{sticker_id}{ext}"
    path.write_bytes(content)
    return str(path.resolve())


class StickerStore:
    """按 session 持久化群聊/私聊里收集到的表情包。"""

    def __init__(
        self,
        sticker_dir: str | Path,
        *,
        max_per_session: int = 200,
    ) -> None:
        self.sticker_dir = Path(sticker_dir)
        self.sticker_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir = self.sticker_dir / "files"
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.max_per_session = max(10, max_per_session)
        self._lock = Lock()

    def _path(self, session_id: str) -> Path:
        safe = session_id_to_fs_key(session_id)
        return self.sticker_dir / f"{safe}.json"

    def _load_raw(self, session_id: str) -> list[dict[str, Any]]:
        path = self._path(session_id)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        return data if isinstance(data, list) else []

    def load(self, session_id: str) -> list[StickerRecord]:
        records: list[StickerRecord] = []
        for item in self._load_raw(session_id):
            if not isinstance(item, dict):
                continue
            seg_type = item.get("segment_type")
            seg_data = item.get("segment_data")
            sticker_id = item.get("id")
            if (
                not isinstance(seg_type, str)
                or not isinstance(seg_data, dict)
                or not isinstance(sticker_id, str)
            ):
                continue
            records.append(
                StickerRecord(
                    id=sticker_id,
                    segment_type=seg_type,
                    segment_data=seg_data,
                    context_text=str(item.get("context_text", "")),
                    keywords=[str(k) for k in item.get("keywords", []) if isinstance(k, str)],
                    use_count=int(item.get("use_count", 0) or 0),
                    collected_at=str(item.get("collected_at", "")),
                    source_user=str(item.get("source_user", "")),
                    local_path=str(item.get("local_path", "")),
                )
            )
        return records

    def save(self, session_id: str, records: list[StickerRecord]) -> None:
        trimmed = records[-self.max_per_session :]
        payload = [asdict(record) for record in trimmed]
        path = self._path(session_id)
        with self._lock:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def count(self, session_id: str) -> int:
        return len(self.load(session_id))

    async def collect_from_event(
        self,
        event: MessageEvent,
        *,
        session_id: str,
        context_text: str,
        source_user: str,
        bot: Bot | None = None,
    ) -> int:
        segments = extract_sticker_segments(event.message)
        if not segments:
            return 0

        records = self.load(session_id)
        index = {record.id: record for record in records}
        added = 0
        keywords = extract_keywords(context_text)
        changed = False

        for seg_type, seg_data in segments:
            sticker_id = _dedupe_key(seg_type, seg_data)
            if sticker_id in index:
                existing = index[sticker_id]
                if context_text and context_text not in existing.context_text:
                    merged = f"{existing.context_text} {context_text}".strip()
                    existing.context_text = merged[:200]
                    changed = True
                for kw in keywords:
                    if kw not in existing.keywords:
                        existing.keywords.append(kw)
                        changed = True
                if not existing.local_path:
                    local = await materialize_sticker_file(
                        bot,
                        sticker_id=sticker_id,
                        segment_type=seg_type,
                        segment_data=seg_data,
                        files_dir=self.files_dir,
                    )
                    if local:
                        existing.local_path = local
                        changed = True
                continue

            local = await materialize_sticker_file(
                bot,
                sticker_id=sticker_id,
                segment_type=seg_type,
                segment_data=seg_data,
                files_dir=self.files_dir,
            )
            record = StickerRecord(
                id=sticker_id,
                segment_type=seg_type,
                segment_data=seg_data,
                context_text=context_text[:200],
                keywords=keywords[:20],
                collected_at=_utc_now(),
                source_user=source_user,
                local_path=local,
            )
            records.append(record)
            index[sticker_id] = record
            added += 1
            changed = True

        if changed:
            self.save(session_id, records)
        return added

    def pick(
        self,
        session_id: str,
        *,
        user_text: str,
        reply_text: str,
        reply_prob: float,
        force: bool = False,
        extra_query: str = "",
    ) -> StickerRecord | None:
        records = self.load(session_id)
        if not records:
            return None
        if not force and random.random() >= max(0.0, min(1.0, reply_prob)):
            return None

        # 优先可复现的可爱贴纸（本地图 / mface / image），系统小黄脸垫底
        preferred = [
            r
            for r in records
            if r.segment_type in {"image", "mface"}
            or (r.local_path and Path(r.local_path).is_file())
        ]
        pool = preferred or records

        query_terms = extract_keywords(user_text, reply_text, extra_query)
        scored = [(score_sticker(record, query_terms), record) for record in pool]
        scored.sort(key=lambda item: item[0], reverse=True)

        best_score, best_record = scored[0]
        if best_score > 0.25 or force:
            chosen = best_record
        else:
            chosen = random.choice(pool)

        chosen.use_count += 1
        updated = [r if r.id != chosen.id else chosen for r in records]
        self.save(session_id, updated)
        return chosen


_store: StickerStore | None = None


def get_sticker_store(config: PluginConfig | None = None) -> StickerStore:
    global _store
    cfg = config or get_config()
    if _store is None:
        _store = StickerStore(
            cfg.sticker_dir,
            max_per_session=cfg.sticker_max_per_session,
        )
    return _store


def maybe_pick_sticker_reply(
    session_id: str,
    *,
    user_text: str,
    reply_text: str,
    interrupt: bool,
    config: PluginConfig | None = None,
    force: bool = False,
    extra_query: str = "",
) -> MessageSegment | None:
    cfg = config or get_config()
    if not cfg.sticker_enable:
        return None
    prob = cfg.sticker_reply_prob_interrupt if interrupt else cfg.sticker_reply_prob
    store = get_sticker_store(cfg)
    record = store.pick(
        session_id,
        user_text=user_text,
        reply_text=reply_text,
        reply_prob=prob,
        force=force,
        extra_query=extra_query,
    )
    return record.to_segment() if record else None
