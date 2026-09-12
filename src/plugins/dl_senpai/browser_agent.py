"""浏览器逛网站：截图 / 淘宝搜商品 / Pixiv 搜图 / 通用图片搜索。

模型写 <<<BROWSE ...>>> 后由 Playwright 执行，图片通过 QQ 发出。
可选依赖：pip install -e ".[browser]" && playwright install chromium
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageSegment

from .config import PluginConfig, get_config

_MARKER_CLOSE = r"(?:>>>|>>|＞＞＞|＞＞|】】|】>|】)"
_BROWSE_MARKER_RE = re.compile(
    rf"<<<BROWSE\s*(.+?)\s*{_MARKER_CLOSE}",
    re.IGNORECASE | re.DOTALL,
)
_BROWSE_MARKER_FALLBACK_RE = re.compile(
    rf"<<<BROWSE[\s\S]*?{_MARKER_CLOSE}",
    re.IGNORECASE,
)
_BROWSE_MARKER_TAIL_RE = re.compile(r"<<<BROWSE\s*[^\n]*$", re.IGNORECASE | re.MULTILINE)
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_BROWSE_INTENT_RE = re.compile(
    r"(打开|去|上|逛|进|访问).{0,16}(淘宝|taobao|pixiv|p站|网站|网页|网址|链接|页面)"
    r"|(淘宝|taobao).{0,24}(搜|找|查|看看)"
    r"|(pixiv|p站|皮克斯维).{0,24}(搜|找|查|壁纸|插画|图)"
    r"|(截图|截屏|截一张|截个图).{0,24}(网页|网站|页面|网址|链接)?"
    r"|帮我(去)?(找|搜).{0,24}(衣服|卫衣|裙子|外套|壁纸|插画|图片|图)"
    r"|找(一?张|个)?(壁纸|插画|好看.{0,6}图)"
    r"|(看看|浏览).{0,12}(淘宝|pixiv|p站|网页|网站)",
    re.IGNORECASE,
)
_BROWSE_NEGATE_RE = re.compile(
    r"(别|不要|不用|无需).{0,8}(打开|逛|截图|淘宝|pixiv|网页)"
    r"|随便聊|闲聊",
    re.IGNORECASE,
)
_TAOBAO_HINT_RE = re.compile(r"淘宝|taobao|衣服|卫衣|裙子|外套|连衣裙|jk|JK|汉服", re.I)
_PIXIV_HINT_RE = re.compile(r"pixiv|p站|皮克斯维|插画|壁纸", re.I)
_PIXIV_MONSTER_RE = re.compile(
    r"(冰呪龙|煌黑龙|灭尽龙|钢龙|炎王龙|雷狼龙|角龙|雌火龙|"
    r"Velkhana|Fatalis|Rathalos|Nergigante)",
    re.I,
)
_SCREENSHOT_HINT_RE = re.compile(r"截图|截屏|截一张|截个图|打开.{0,8}(网址|链接|网页)", re.I)

_DEFAULT_ALLOWED_SUFFIXES = (
    "pixiv.net",
    "taobao.com",
    "tmall.com",
    "alicdn.com",
    "bing.com",
    "google.com",
    "baidu.com",
    "bilibili.com",
    "weibo.com",
    "zhihu.com",
    "jd.com",
    "1688.com",
    "yangkeduo.com",
    "pinduoduo.com",
)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

_browser_lock = asyncio.Lock()


class BrowseError(Exception):
    """浏览器任务失败。"""


@dataclass(frozen=True)
class BrowseTask:
    kind: str
    query: str = ""
    url: str = ""


@dataclass
class BrowseRequest:
    text: str
    tasks: list[BrowseTask] = field(default_factory=list)
    force: bool = False


@dataclass
class BrowseResult:
    ok: bool
    message: str = ""
    image_paths: list[Path] = field(default_factory=list)
    page_url: str = ""


class BrowseCooldown:
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


def browser_available() -> bool:
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


def browser_configured(config: PluginConfig | None = None) -> bool:
    cfg = config or get_config()
    return bool(cfg.browser_enable and browser_available())


def parse_browse_request(raw_reply: str) -> BrowseRequest:
    text = raw_reply or ""
    tasks: list[BrowseTask] = []
    seen: set[str] = set()
    force = False
    for match in _BROWSE_MARKER_RE.finditer(text):
        force = True
        part = (match.group(1) or "").strip()
        task = _parse_task_line(part)
        if task is None:
            continue
        key = f"{task.kind}|{task.query}|{task.url}".casefold()
        if key in seen:
            continue
        seen.add(key)
        tasks.append(task)
    cleaned = _BROWSE_MARKER_RE.sub("", text)
    cleaned = _BROWSE_MARKER_FALLBACK_RE.sub("", cleaned)
    cleaned = _BROWSE_MARKER_TAIL_RE.sub("", cleaned)
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return BrowseRequest(text=cleaned, tasks=tasks[:1], force=force)


def strip_browse_markers(text: str) -> str:
    cleaned = _BROWSE_MARKER_RE.sub("", text or "")
    cleaned = _BROWSE_MARKER_FALLBACK_RE.sub("", cleaned)
    cleaned = _BROWSE_MARKER_TAIL_RE.sub("", cleaned)
    return cleaned.strip()


def wants_browser_help(text: str) -> bool:
    raw = (text or "").strip()
    if len(raw) < 4:
        return False
    if _BROWSE_NEGATE_RE.search(raw):
        return False
    if _URL_RE.search(raw) and _SCREENSHOT_HINT_RE.search(raw):
        return True
    return bool(_BROWSE_INTENT_RE.search(raw))


def infer_browse_tasks(user_text: str) -> list[BrowseTask]:
    raw = (user_text or "").strip()
    if not raw:
        return []
    url_match = _URL_RE.search(raw)
    if url_match and (_SCREENSHOT_HINT_RE.search(raw) or "http" in raw.lower()):
        return [BrowseTask(kind="screenshot", url=url_match.group(0).rstrip("，。！？,.!?"))]
    if _TAOBAO_HINT_RE.search(raw):
        query = _extract_search_query(raw, site="taobao")
        if query:
            return [BrowseTask(kind="taobao", query=query)]
    if _PIXIV_HINT_RE.search(raw):
        query = _extract_search_query(raw, site="pixiv")
        if query:
            return [BrowseTask(kind="pixiv", query=query)]
    if _BROWSE_INTENT_RE.search(raw):
        query = _extract_search_query(raw, site="image")
        if query:
            return [BrowseTask(kind="image", query=query)]
    return []


def resolve_browse_tasks(
    *,
    marker_tasks: list[BrowseTask],
    user_request: str,
) -> list[BrowseTask]:
    if marker_tasks:
        return marker_tasks[:1]
    return infer_browse_tasks(user_request)[:1]


def browse_retry_hint(user_request: str) -> str:
    tasks = infer_browse_tasks(user_request)
    if tasks:
        task = tasks[0]
        if task.kind == "screenshot":
            return (
                "对方让你打开/截图网页。请简短回应，并在末尾写："
                f"<<<BROWSE screenshot {task.url}>>>"
            )
        if task.kind == "taobao":
            return (
                "对方让你去淘宝找商品。请简短回应，并在末尾写："
                f"<<<BROWSE taobao {task.query}>>>"
            )
        if task.kind == "pixiv":
            return (
                "对方让你去 Pixiv 找图。请简短回应，并在末尾写："
                f"<<<BROWSE pixiv {task.query}>>>"
            )
        return (
            "对方让你找图/逛网站。请简短回应，并在末尾写："
            f"<<<BROWSE image {task.query}>>>"
        )
    return (
        "对方让你打开网页或搜图。请简短回应，并在末尾写 <<<BROWSE ...>>>。"
        "格式：<<<BROWSE taobao 关键词>>> / <<<BROWSE pixiv 关键词>>> / "
        "<<<BROWSE screenshot https://...>>> / <<<BROWSE image 关键词>>>"
    )


def browse_block_note(reason: str) -> str:
    if reason == "cooldown":
        return "学姐刚逛完网站，歇一会儿再来找我搜哦～"
    if reason == "not_configured":
        return "学姐这边浏览器还没装好，暂时没法帮你逛网站呢……"
    return ""


def browse_failure_note() -> str:
    return "唔……网页这边卡住了，晚点再让学姐帮你逛一次？"


def paths_to_image_segments(paths: list[Path]) -> list[MessageSegment]:
    segments: list[MessageSegment] = []
    for path in paths:
        if path.is_file():
            segments.append(MessageSegment.image(file=path.resolve().as_uri()))
    return segments


async def run_browse_tasks(
    tasks: list[BrowseTask],
    *,
    config: PluginConfig | None = None,
) -> BrowseResult:
    cfg = config or get_config()
    if not tasks:
        return BrowseResult(ok=False, message="任务为空")
    if not browser_configured(cfg):
        return BrowseResult(ok=False, message="browser not configured")
    out_dir = Path(cfg.browser_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    max_images = max(1, int(cfg.browser_max_images))
    paths: list[Path] = []
    page_url = ""
    for task in tasks[:1]:
        try:
            result = await _run_single_task(task, config=cfg, out_dir=out_dir)
        except BrowseError as exc:
            logger.warning(f"dl_senpai browse task failed kind={task.kind}: {exc}")
            return BrowseResult(ok=False, message=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"dl_senpai browse unexpected kind={task.kind}: {exc}")
            return BrowseResult(ok=False, message=str(exc))
        paths.extend(result.image_paths[:max_images])
        page_url = result.page_url or page_url
    if not paths:
        return BrowseResult(ok=False, message="no images captured", page_url=page_url)
    return BrowseResult(ok=True, image_paths=paths, page_url=page_url)


def _parse_task_line(line: str) -> BrowseTask | None:
    raw = (line or "").strip()
    if not raw:
        return None
    lower = raw.lower()
    if lower.startswith("screenshot "):
        url = raw[11:].strip()
        return BrowseTask(kind="screenshot", url=url) if url else None
    if lower.startswith("url "):
        url = raw[4:].strip()
        return BrowseTask(kind="screenshot", url=url) if url else None
    if lower.startswith("taobao "):
        query = raw[7:].strip()
        return BrowseTask(kind="taobao", query=query) if query else None
    if lower.startswith("pixiv "):
        query = raw[6:].strip()
        return BrowseTask(kind="pixiv", query=query) if query else None
    if lower.startswith("image "):
        query = raw[6:].strip()
        return BrowseTask(kind="image", query=query) if query else None
    if raw.startswith(("http://", "https://")):
        return BrowseTask(kind="screenshot", url=raw.rstrip("，。！？,.!?"))
    if "淘宝" in raw or "taobao" in lower:
        query = re.sub(r".*?(淘宝|taobao)[:：\s]*", "", raw, flags=re.I).strip() or raw
        return BrowseTask(kind="taobao", query=query)
    if "pixiv" in lower or "p站" in raw:
        query = re.sub(r".*?(pixiv|p站)[:：\s]*", "", raw, flags=re.I).strip() or raw
        return BrowseTask(kind="pixiv", query=query)
    return BrowseTask(kind="image", query=raw)


def _extract_search_query(text: str, *, site: str) -> str:
    raw = (text or "").strip()
    patterns = [
        r"(?:帮我|请|能不能|可以)?(?:去|上)?(?:淘宝|taobao|pixiv|p站)?(?:搜|找|查)(?:一下|下)?[:：\s]*(.+)",
        r"(?:壁纸|插画|图)[:：\s]*(.+)",
        r"(?:黑色|白色|蓝色|红色|粉色|绿色).{0,20}(?:卫衣|裙子|外套|衣服|连衣裙)",
    ]
    for pat in patterns:
        m = re.search(pat, raw, re.I)
        if m:
            q = (m.group(1) if m.lastindex else m.group(0)).strip()
            q = re.sub(r"[@＠]\S+", "", q).strip()
            q = re.sub(r"^学姐[,，:\s]*", "", q, flags=re.I).strip()
            if len(q) >= 2:
                return q[:80]
    cleaned = re.sub(
        r"[@＠]\S+|学姐|senpai|帮我|请|去|上|淘宝|taobao|pixiv|p站|"
        r"搜|找|查|一下|打开|截图|网页|网站|链接|看看|浏览",
        " ",
        raw,
        flags=re.I,
    )
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，,。.!！?？")
    if len(cleaned) >= 2:
        return cleaned[:80]
    if site == "taobao" and _TAOBAO_HINT_RE.search(raw):
        return "女装" if "女" in raw else "热门商品"
    if site == "pixiv" and _PIXIV_HINT_RE.search(raw):
        if m := _PIXIV_MONSTER_RE.search(raw):
            name = m.group(1) if m.lastindex else m.group()
            return f"{name} 壁纸"
        return "壁纸"
    return ""


def _allowed_domains(config: PluginConfig) -> set[str] | None:
    """None = 未配置白名单，截图允许任意公网 https。"""
    raw = (config.browser_allowed_domains or "").strip()
    if raw == "*":
        return {"*"}
    if raw:
        return {d.strip().lower().lstrip(".") for d in raw.split(",") if d.strip()}
    return None


def _is_private_host(host: str) -> bool:
    host = (host or "").strip().lower()
    if not host or host in {"localhost", "127.0.0.1", "0.0.0.0"}:
        return True
    if host.endswith(".local"):
        return True
    try:
        addr = ipaddress.ip_address(socket.gethostbyname(host.split(":")[0]))
        return bool(addr.is_private or addr.is_loopback or addr.is_link_local)
    except Exception:  # noqa: BLE001
        return False


def _host_allowed(host: str, allowed: set[str] | None) -> bool:
    if allowed is not None and "*" in allowed:
        return True
    host = (host or "").lower().lstrip(".")
    if not host:
        return False
    if allowed is None:
        return not _is_private_host(host)
    for domain in allowed:
        if host == domain or host.endswith("." + domain):
            return True
    for domain in _DEFAULT_ALLOWED_SUFFIXES:
        if host == domain or host.endswith("." + domain):
            return True
    return False


def _validate_url(url: str, config: PluginConfig) -> str:
    raw = (url or "").strip()
    if not raw.startswith(("http://", "https://")):
        raise BrowseError("网址格式不对，需要以 http:// 或 https:// 开头")
    parsed = urlparse(raw)
    host = parsed.netloc or ""
    if not _host_allowed(host, _allowed_domains(config)):
        raise BrowseError(f"这个网站不在允许列表里：{host}")
    return raw


def _task_digest(task: BrowseTask) -> str:
    blob = f"{task.kind}|{task.query}|{task.url}"
    return hashlib.sha256(blob.encode()).hexdigest()[:10]


async def _run_single_task(
    task: BrowseTask,
    *,
    config: PluginConfig,
    out_dir: Path,
) -> BrowseResult:
    kind = (task.kind or "image").lower()
    digest = _task_digest(task)
    if kind == "screenshot":
        url = _validate_url(task.url, config)
        path = out_dir / f"shot_{digest}.png"
        await _screenshot_url(url, path, config=config)
        return BrowseResult(ok=True, image_paths=[path], page_url=url)
    if kind == "taobao":
        if not task.query.strip():
            raise BrowseError("淘宝搜索关键词为空")
        return await _browse_taobao(task.query.strip(), out_dir, digest, config)
    if kind == "pixiv":
        if not task.query.strip():
            raise BrowseError("Pixiv 搜索关键词为空")
        return await _browse_pixiv(task.query.strip(), out_dir, digest, config)
    if not task.query.strip():
        raise BrowseError("搜图关键词为空")
    return await _browse_image_search(task.query.strip(), out_dir, digest, config)


async def _screenshot_url(url: str, dest: Path, *, config: PluginConfig) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)

    async def _work(page: Any) -> None:
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(1500)
        await page.screenshot(path=str(dest), full_page=False, type="png")

    await _with_page(config, _work)


async def _browse_taobao(
    query: str,
    out_dir: Path,
    digest: str,
    config: PluginConfig,
) -> BrowseResult:
    url = f"https://s.taobao.com/search?q={quote(query)}"
    shot = out_dir / f"taobao_{digest}.png"
    extra_paths: list[Path] = []

    async def _work(page: Any) -> None:
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(shot), full_page=False, type="png")
        # 尝试抓取商品缩略图
        loc = page.locator("img[src*='alicdn'], img[src*='taobao']").first
        try:
            if await loc.count() > 0:
                thumb = out_dir / f"taobao_item_{digest}.png"
                await loc.screenshot(path=str(thumb), type="png")
                if thumb.is_file() and thumb.stat().st_size > 1024:
                    extra_paths.append(thumb)
        except Exception:  # noqa: BLE001
            pass

    await _with_page(config, _work)
    paths = [shot]
    paths.extend(extra_paths)
    return BrowseResult(ok=True, image_paths=paths, page_url=url)


async def _browse_pixiv(
    query: str,
    out_dir: Path,
    digest: str,
    config: PluginConfig,
) -> BrowseResult:
    url = f"https://www.pixiv.net/tags/{quote(query)}/artworks"
    shot = out_dir / f"pixiv_{digest}.png"
    extra_paths: list[Path] = []

    async def _work(page: Any) -> None:
        await page.goto(url, wait_until="domcontentloaded")
        await page.wait_for_timeout(3500)
        await page.screenshot(path=str(shot), full_page=False, type="png")
        # 尝试打开第一张作品
        link = page.locator('a[href*="/artworks/"]').first
        try:
            if await link.count() > 0:
                href = await link.get_attribute("href")
                if href:
                    art_url = href if href.startswith("http") else f"https://www.pixiv.net{href}"
                    await page.goto(art_url, wait_until="domcontentloaded")
                    await page.wait_for_timeout(2500)
                    art_shot = out_dir / f"pixiv_art_{digest}.png"
                    await page.screenshot(path=str(art_shot), full_page=False, type="png")
                    if art_shot.is_file():
                        extra_paths.append(art_shot)
                    img = page.locator(
                        "img[src*='pximg'], img[src*='pixiv'], canvas, picture img"
                    ).first
                    if await img.count() > 0:
                        art_img = out_dir / f"pixiv_img_{digest}.png"
                        await img.screenshot(path=str(art_img), type="png")
                        if art_img.is_file() and art_img.stat().st_size > 2048:
                            extra_paths.append(art_img)
        except Exception:  # noqa: BLE001
            pass

    await _with_page(config, _work)
    paths = list(extra_paths) if extra_paths else [shot]
    if shot.is_file() and shot not in paths:
        paths.insert(0, shot)
    return BrowseResult(ok=True, image_paths=paths, page_url=url)


async def _browse_image_search(
    query: str,
    out_dir: Path,
    digest: str,
    config: PluginConfig,
) -> BrowseResult:
    hits = await _search_images(query, limit=max(2, int(config.browser_max_images)))
    if not hits:
        # 兜底：Bing 图片搜索页截图
        url = f"https://www.bing.com/images/search?q={quote(query)}"
        shot = out_dir / f"bing_img_{digest}.png"

        async def _work(page: Any) -> None:
            await page.goto(url, wait_until="domcontentloaded")
            await page.wait_for_timeout(2500)
            await page.screenshot(path=str(shot), full_page=False, type="png")

        await _with_page(config, _work)
        return BrowseResult(ok=True, image_paths=[shot], page_url=url)

    paths: list[Path] = []
    for idx, hit in enumerate(hits):
        dest = out_dir / f"img_{digest}_{idx}.jpg"
        ok = await _download_image(hit, dest, timeout=config.browser_timeout)
        if ok:
            paths.append(dest)
    if paths:
        return BrowseResult(ok=True, image_paths=paths, page_url=hits[0].get("page_url", ""))
    raise BrowseError("图片下载失败")


async def _search_images(query: str, *, limit: int = 3) -> list[dict[str, str]]:
    q = (query or "").strip()
    if not q:
        return []

    def _run_ddgs() -> list[dict[str, str]]:
        rows_out: list[dict[str, str]] = []
        try:
            from ddgs import DDGS  # type: ignore[import-untyped]
        except ImportError:
            try:
                from duckduckgo_search import DDGS  # type: ignore[import-untyped]
            except ImportError:
                return []
        with DDGS() as ddgs:
            try:
                rows = ddgs.images(
                    q,
                    region="cn-zh",
                    safesearch="moderate",
                    max_results=limit,
                )
            except TypeError:
                rows = ddgs.images(
                    keywords=q,
                    region="cn-zh",
                    safesearch="moderate",
                    max_results=limit,
                )
            for row in rows or []:
                if not isinstance(row, dict):
                    continue
                image_url = str(row.get("image") or row.get("thumbnail") or "").strip()
                if not image_url.startswith(("http://", "https://")):
                    continue
                rows_out.append(
                    {
                        "image_url": image_url,
                        "page_url": str(row.get("url") or row.get("source") or image_url),
                        "title": str(row.get("title") or q),
                    }
                )
                if len(rows_out) >= limit:
                    break
        return rows_out

    return await asyncio.to_thread(_run_ddgs)


async def _download_image(hit: dict[str, str], dest: Path, *, timeout: float) -> bool:
    url = str(hit.get("image_url") or "").strip()
    if not url:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    headers = {"User-Agent": _UA, "Referer": str(hit.get("page_url") or url)}
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers=headers,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
        return dest.is_file() and dest.stat().st_size > 512
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai browse download failed url={url[:72]} err={exc}")
        return False


async def _with_page(config: PluginConfig, fn: Any) -> Any:
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise BrowseError(
            '未安装浏览器依赖，请运行: pip install -e ".[browser]" && playwright install chromium'
        ) from exc

    async with _browser_lock:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=bool(config.browser_headless))
            try:
                context = await browser.new_context(
                    viewport={
                        "width": int(config.browser_viewport_width),
                        "height": int(config.browser_viewport_height),
                    },
                    user_agent=_UA,
                    locale="zh-CN",
                )
                page = await context.new_page()
                page.set_default_timeout(int(float(config.browser_timeout) * 1000))
                return await fn(page)
            finally:
                await browser.close()
