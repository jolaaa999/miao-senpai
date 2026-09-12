from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Iterable

import httpx

if TYPE_CHECKING:
    from .config import PluginConfig

try:
    from nonebot import logger
except Exception:  # noqa: BLE001 — MCP 进程可能无 nonebot

    class _Logger:
        def warning(self, msg: str) -> None:
            print(msg)

        def info(self, msg: str) -> None:
            print(msg)

    logger = _Logger()  # type: ignore[assignment]

DEFAULT_SOURCES = ("weibo", "douyin", "bilibili", "baidu", "zhihu")
SOURCE_LABELS = {
    "weibo": "微博",
    "douyin": "抖音",
    "bilibili": "B站",
    "baidu": "百度",
    "zhihu": "知乎",
}


@dataclass(frozen=True)
class HotItem:
    source: str
    title: str
    rank: int = 0
    url: str = ""
    hot: str = ""


@dataclass
class TrendsSnapshot:
    fetched_at: float
    items: list[HotItem]
    base_url: str = ""
    stale: bool = False


class TrendsStore:
    """热榜采集 + 磁盘 TTL 缓存（学姐 / MCP 共用）。"""

    def __init__(
        self,
        *,
        cache_dir: str | Path = "data/dl_senpai/trends",
        base_url: str = "https://uapis.cn/api/v1/misc/hotboard",
        sources: Iterable[str] | None = None,
        ttl_sec: int = 1800,
        per_source_limit: int = 8,
        timeout: float = 12.0,
        fallback_bases: Iterable[str] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_path = self.cache_dir / "cache.json"
        self.base_url = base_url.rstrip("/")
        self.fallback_bases = tuple(
            b.strip().rstrip("/")
            for b in (fallback_bases or ())
            if str(b).strip()
        )
        self.sources = tuple(
            s.strip().lower()
            for s in (sources or DEFAULT_SOURCES)
            if str(s).strip()
        ) or DEFAULT_SOURCES
        self.ttl_sec = max(60, int(ttl_sec))
        self.per_source_limit = max(1, int(per_source_limit))
        self.timeout = timeout
        self._lock = Lock()
        self._mem: TrendsSnapshot | None = None
        self._refresh_lock = asyncio.Lock()

    def _load_disk(self) -> TrendsSnapshot | None:
        if not self.cache_path.exists():
            return None
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        items_raw = raw.get("items")
        if not isinstance(items_raw, list):
            return None
        items: list[HotItem] = []
        for row in items_raw:
            if not isinstance(row, dict):
                continue
            title = str(row.get("title") or "").strip()
            source = str(row.get("source") or "").strip()
            if not title or not source:
                continue
            items.append(
                HotItem(
                    source=source,
                    title=title,
                    rank=int(row.get("rank") or 0),
                    url=str(row.get("url") or ""),
                    hot=str(row.get("hot") or ""),
                )
            )
        fetched_at = float(raw.get("fetched_at") or 0)
        if not items or fetched_at <= 0:
            return None
        return TrendsSnapshot(
            fetched_at=fetched_at,
            items=items,
            base_url=str(raw.get("base_url") or ""),
            stale=False,
        )

    def _save_disk(self, snap: TrendsSnapshot) -> None:
        payload = {
            "fetched_at": snap.fetched_at,
            "base_url": snap.base_url,
            "items": [asdict(i) for i in snap.items],
        }
        with self._lock:
            self.cache_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def _fresh(self, snap: TrendsSnapshot | None, now: float | None = None) -> bool:
        if snap is None:
            return False
        now = time.time() if now is None else now
        return (now - snap.fetched_at) < self.ttl_sec

    @staticmethod
    def parse_source_payload(source: str, payload: Any, *, limit: int) -> list[HotItem]:
        if not isinstance(payload, dict):
            return []
        data = payload.get("data")
        if not isinstance(data, list):
            data = payload.get("list")
        if not isinstance(data, list):
            return []
        out: list[HotItem] = []
        for idx, row in enumerate(data[:limit], start=1):
            if isinstance(row, str):
                title = row.strip()
                url = ""
                hot = ""
            elif isinstance(row, dict):
                title = str(row.get("title") or row.get("name") or "").strip()
                url = str(row.get("url") or row.get("mobileUrl") or "").strip()
                hot = str(
                    row.get("hot")
                    or row.get("hot_value")
                    or row.get("desc")
                    or ""
                ).strip()
                if row.get("index") is not None:
                    try:
                        idx = int(row["index"])
                    except (TypeError, ValueError):
                        pass
            else:
                continue
            if not title:
                continue
            out.append(HotItem(source=source, title=title, rank=idx, url=url, hot=hot))
        return out

    def _request_for_source(self, source: str) -> tuple[str, dict[str, str]]:
        """兼容 DailyHot `/{source}` 与 uapis `?type=`。"""
        base = self.base_url.rstrip("/")
        lower = base.lower()
        if "uapis.cn" in lower or lower.endswith("/hotboard"):
            return base, {"type": source}
        return f"{base}/{source}", {}

    async def _fetch_source(
        self,
        client: httpx.AsyncClient,
        source: str,
    ) -> list[HotItem]:
        url, params = self._request_for_source(source)
        try:
            resp = await client.get(url, params=params or None)
        except ValueError as e:
            # anyio 在 Windows 上连接失败且 oserrors 为空时会抛
            # ValueError("second argument (exceptions) must be a non-empty sequence")，
            # 归一成常规连接错误，避免打穿 gather
            raise ConnectionError(f"trends fetch failed: {url}") from e
        resp.raise_for_status()
        return self.parse_source_payload(
            source, resp.json(), limit=self.per_source_limit
        )

    async def refresh(self) -> TrendsSnapshot:
        bases = (self.base_url, *self.fallback_bases)
        last_errors: list[str] = []
        for base in bases:
            if not base:
                continue
            self.base_url = base
            items: list[HotItem] = []
            seen: set[str] = set()
            errors: list[str] = []
            async with httpx.AsyncClient(
                timeout=self.timeout, follow_redirects=True
            ) as client:
                results = await asyncio.gather(
                    *[self._fetch_source(client, s) for s in self.sources],
                    return_exceptions=True,
                )
            for source, result in zip(self.sources, results, strict=True):
                if isinstance(result, BaseException):
                    errors.append(f"{source}:{result}")
                    continue
                for item in result:
                    key = item.title.casefold()
                    if key in seen:
                        continue
                    seen.add(key)
                    items.append(item)
            if items:
                snap = TrendsSnapshot(
                    fetched_at=time.time(),
                    items=items,
                    base_url=base,
                    stale=False,
                )
                self._mem = snap
                try:
                    self._save_disk(snap)
                except OSError:
                    logger.warning("dl_senpai trends: failed to write cache")
                if errors:
                    logger.warning(f"dl_senpai trends partial errors: {errors}")
                return snap
            last_errors = errors or [f"{base}: empty"]
            logger.warning(f"dl_senpai trends base failed: {base} ({last_errors})")
        raise RuntimeError(
            "trends refresh failed: " + ("; ".join(last_errors) or "empty")
        )

    async def get_snapshot(self, *, force: bool = False) -> TrendsSnapshot | None:
        if not force and self._fresh(self._mem):
            return self._mem
        if not force:
            disk = self._mem or self._load_disk()
            if self._fresh(disk):
                self._mem = disk
                return disk
        async with self._refresh_lock:
            if not force and self._fresh(self._mem):
                return self._mem
            try:
                return await self.refresh()
            except Exception as e:  # noqa: BLE001
                logger.warning(f"dl_senpai trends refresh failed: {e}")
                fallback = self._mem or self._load_disk()
                if fallback is None:
                    return None
                stale = TrendsSnapshot(
                    fetched_at=fallback.fetched_at,
                    items=fallback.items,
                    base_url=fallback.base_url,
                    stale=True,
                )
                self._mem = stale
                return stale

    async def get_items(
        self,
        *,
        source: str | None = None,
        limit: int | None = None,
        force: bool = False,
    ) -> list[HotItem]:
        snap = await self.get_snapshot(force=force)
        if snap is None:
            return []
        items = snap.items
        if source:
            src = source.strip().lower()
            items = [i for i in items if i.source == src]
        if limit is not None:
            items = items[: max(0, limit)]
        return items


def format_trends_for_prompt(
    items: list[HotItem],
    *,
    max_items: int = 20,
    max_chars: int = 800,
    stale: bool = False,
) -> str:
    if not items:
        return ""
    lines = ["【近期热梗备忘】（标题级，可能滞后；可轻度接梗，别硬蹭，别编细节）"]
    if stale:
        lines.append("（缓存偏旧，点到为止）")
    count = 0
    for item in items:
        if count >= max_items:
            break
        label = SOURCE_LABELS.get(item.source, item.source)
        line = f"- [{label}] {item.title}"
        tentative = "\n".join(lines + [line])
        if max_chars > 0 and len(tentative) > max_chars:
            break
        lines.append(line)
        count += 1
    if count == 0:
        return ""
    return "\n".join(lines)


_store: TrendsStore | None = None


def get_trends_store(config: PluginConfig | None = None) -> TrendsStore:
    """进程内单例（从 PluginConfig 读参）。测试请直接构造 TrendsStore。"""
    global _store
    if _store is None:
        if config is None:
            from .config import get_config

            config = get_config()
        _store = TrendsStore(
            cache_dir=config.trends_cache_dir,
            base_url=config.trends_base_url,
            sources=config.trends_source_list(),
            ttl_sec=config.trends_ttl_sec,
            per_source_limit=config.trends_per_source,
            fallback_bases=config.trends_fallback_list(),
        )
    return _store


def reset_trends_store() -> None:
    global _store
    _store = None


async def get_hot_items(
    *,
    source: str | None = None,
    limit: int | None = None,
    force: bool = False,
    store: TrendsStore | None = None,
) -> list[HotItem]:
    st = store or get_trends_store()
    return await st.get_items(source=source, limit=limit, force=force)


async def get_trends_brief(
    *,
    max_items: int = 20,
    max_chars: int = 800,
    interrupt: bool = False,
    store: TrendsStore | None = None,
) -> str:
    st = store or get_trends_store()
    snap = await st.get_snapshot()
    if snap is None or not snap.items:
        return ""
    limit = max(5, max_items // 2) if interrupt else max_items
    chars = max(200, max_chars // 2) if interrupt else max_chars
    return format_trends_for_prompt(
        snap.items,
        max_items=limit,
        max_chars=chars,
        stale=snap.stale,
    )
