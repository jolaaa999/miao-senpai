"""读链接：把对方甩过来的那个 URL 变成她真能读到的正文。

为什么要单独一层
----------------
她之前「看到链接」的方式只有两种：BROWSE 截图（图里文字她读不到），
或者 fetch_page_excerpt()（那是搜索的佐证，限 500 字、不跑 JS、模型也调不到）。
结果是群里甩个链接问「这啥」，她只能顺着 URL 里的英文单词猜——猜错比不答更糟。

这一层要做的事和 DSH 抓网页时一样，四步：

1. **先白拿**：httpx 手动跟跳转（能拿到最终 URL）、限体积、按 content-type 分派。
2. **抽不干净再上浏览器**：SPA（小黑盒、知乎、B站专栏）静态 HTML 是空壳，
   正文不到 300 字就用 Playwright 渲染一遍再取 innerText。项目里已经有
   browser_agent._with_page，不重复造。
3. **切片不撒谎**：超长就截断，但要在结果里写明「共 N 字，已截断」——
   否则她会把前半篇当成全文去下结论。
4. **长文先摘要**：几千字塞进上下文既贵又稀释注意力，超过阈值先用一次模型压成要点。

安全（她这个场景比 DSH 更危险：群里任何人都能触发）
----------------------------------------------------
- **禁内网**：群友发个 http://192.168.1.1 就能让机器人替他在内网里发请求。
  所以每一跳（含重定向）都要查一遍：IP 字面量 + DNS 解析结果都要是公网地址。
- **只读**：不带 cookie、不带头、不 POST、不下载附件、不落盘原始文件。
- **体积/跳数/时限**：2MB、3 跳、20 秒，超了就明确失败，不硬撑。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse, urlsplit

import httpx
from nonebot import logger

__all__ = [
    "LinkDoc",
    "auto_read_brief",
    "extract_first_url",
    "host_blocked_reason",
    "read_link",
    "render_brief",
    "resolve_blocked_reason",
]

# 体积上限。为什么不设更大：大于 2MB 的「网页」基本都是附件或打包数据，
# 抽正文没有意义，还容易把进程吃满。
MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 3
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_URL_RE = re.compile(r"https?://[^\s<>\"'）)】\]]+", re.IGNORECASE)
# 这些后缀背后是文件而不是文章，直接读没有意义（图片/视频/压缩包/安装包）
_ASSET_EXT = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg",
    ".mp4", ".mkv", ".avi", ".mp3", ".flac", ".wav",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".exe", ".msi", ".apk", ".dmg", ".iso",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
)
# 静态 HTML 抽不出这么多字，就当它是空壳，准备上浏览器
_EMPTY_TEXT_THRESHOLD = 300
# 低于这个字数就算「没读到正文」：通常只剩站点名或一句 cookie 提示
_MIN_USABLE_CHARS = 120

# 只剩页头页脚的页面不算读到。实测小黑盒/知乎的帖子页在渲染完成前会先
# 吐一屏「导航 + 备案号 + 版权」，长度能过 _MIN_USABLE_CHARS，
# 于是被判成"读到了"——她去答"这帖子讲了啥"就只能编。
# 判据：出现两条以上站点标识，且总长度又短。
_CHROME_MARKERS = (
    "ICP备",
    "公网安备",
    "违法和不良信息举报",
    "All Rights Reserved",
    "立即下载小黑盒APP",
)


# 站点 -> 登录态/ cookie 罐的键。只列"确实需要身份才看得见正文"的站：
# 小黑盒的帖子接口对陌生访客会直接回 show_captcha，带上同一份 cookie 罐
# （哪怕没登录）就不那么容易被当成新访客。
_SITE_BY_HOST = (
    ("xiaoheihe", "heybox"),
)


def _site_of_url(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    for needle, site in _SITE_BY_HOST:
        if needle in host:
            return site
    return ""


def _looks_like_chrome(text: str) -> bool:
    """这段"正文"其实是站点导航/备案号吗？"""
    body = (text or "").strip()
    if len(body) > 800:
        return False
    return sum(1 for marker in _CHROME_MARKERS if marker in body) >= 2
_TITLE_MAX = 120


@dataclass
class LinkDoc:
    """一次读链接的结果。失败也是一种结果，所以 error 是字段而不是异常。"""

    url: str
    final_url: str = ""
    title: str = ""
    text: str = ""
    source: str = ""          # http / browser / pdf / plain
    truncated: bool = False
    total_chars: int = 0
    error: str = ""           # timeout / blocked_private / http_4xx / empty_text ...
    note: str = ""
    cached: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.text.strip())


def extract_first_url(text: str) -> str:
    """从一句话里挑出第一个「像是文章」的链接。没有则空串。

    顺手排除图片/视频/压缩包：那些链接她没法读，当没看见比读失败好——
    读失败会让她在群里回一句「我读不到」，而对方本来就是发图。
    """
    for match in _URL_RE.finditer(text or ""):
        raw = match.group(0).rstrip("。，、；！？.,;!?")
        lowered = raw.lower().split("?")[0]
        if any(lowered.endswith(ext) for ext in _ASSET_EXT):
            continue
        return raw
    return ""


# ------------------------------------------------------------------ 安全围栏


def _ip_blocked_reason(ip_text: str) -> str:
    """判断单个 IP 是否允许访问。返回空串=允许。"""
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return ""
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
        return f"内网/保留地址（{ip_text}）"
    if ip.is_multicast or ip.is_unspecified:
        return f"非单播地址（{ip_text}）"
    return ""


_BLOCKED_HOST_SUFFIX = (".local", ".internal", ".localhost", ".home.arpa")
_BLOCKED_HOSTS = {
    "localhost",
    "metadata.google.internal",
    "metadata",
    "instance-data",
}


def host_blocked_reason(host: str) -> str:
    """主机名层的黑名单（不查 DNS 的那一半）。"""
    name = (host or "").strip().strip("[]").lower()
    if not name:
        return "空主机名"
    if name in _BLOCKED_HOSTS:
        return f"本机/元数据地址（{name}）"
    if name.endswith(_BLOCKED_HOST_SUFFIX):
        return f"本地域名（{name}）"
    literal = _ip_blocked_reason(name.rstrip("."))
    if literal:
        return literal
    return ""


async def resolve_blocked_reason(host: str) -> str:
    """解析一次 DNS，只要有一个解析结果是内网就拒。

    为什么必须做：攻击者可以用一个公网域名解析到 127.0.0.1（DNS rebinding 的
    简化版），只看域名是拦不住的。这里不追求完美防护，但挡住了绝大多数。
    """
    literal = host_blocked_reason(host)
    if literal:
        return literal
    name = (host or "").strip().strip("[]")
    if not name:
        return "空主机名"
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, name, None)
    except Exception:  # noqa: BLE001 — 解析不了就让真正的请求去报错
        return ""
    for info in infos:
        addr = info[4][0]
        reason = _ip_blocked_reason(addr)
        if reason:
            return f"{name} 解析到{reason}"
    return ""


def _allowed_domains(cfg: Any) -> set[str] | None:
    """白名单。留空 = 除内网外都可以（默认）。"""
    raw = str(getattr(cfg, "link_read_allowed_domains", "") or "")
    items = {part.strip().lower() for part in re.split(r"[,\s]+", raw) if part.strip()}
    return items or None


def _host_allowed(host: str, allowed: set[str] | None) -> bool:
    if allowed is None:
        return True
    name = (host or "").strip().lower()
    for rule in allowed:
        if name == rule or name.endswith("." + rule.lstrip(".")):
            return True
    return False


async def validate_url(url: str, cfg: Any) -> str:
    """返回空串=允许；否则返回拒因。"""
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return "URL 解析失败"
    if parsed.scheme not in ("http", "https"):
        return f"只支持 http/https（{parsed.scheme or '空'}）"
    if not parsed.hostname:
        return "没有主机名"
    if not _host_allowed(parsed.hostname, _allowed_domains(cfg)):
        return f"域名不在白名单里（{parsed.hostname}）"
    return await resolve_blocked_reason(parsed.hostname)


# ------------------------------------------------------------------ 抓取


async def _follow_http(url: str, cfg: Any) -> tuple[Any, str, str]:
    """返回 (payload, content_type, final_url)。payload 是 str 或 bytes。

    手动跟跳转而不是 follow_redirects=True：每一跳都要重新过一遍内网检查，
    否则一个公网短链 302 到 http://127.0.0.1 就把围栏绕过去了。
    """
    timeout = float(getattr(cfg, "link_read_timeout", 20.0) or 20.0)
    max_bytes = int(getattr(cfg, "link_read_max_bytes", MAX_BYTES) or MAX_BYTES)
    current = url
    headers = {
        "User-Agent": _UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
    }
    async with httpx.AsyncClient(timeout=timeout, headers=headers, follow_redirects=False) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            reason = await validate_url(current, cfg)
            if reason:
                raise PermissionError(reason)
            resp = await client.get(current)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location") or ""
                if not location:
                    raise httpx.HTTPStatusError("跳转没有 location", request=resp.request, response=resp)
                current = urljoin(current, location)
                continue
            if resp.status_code >= 400:
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}", request=resp.request, response=resp
                )
            ctype = (resp.headers.get("content-type") or "").lower()
            raw = resp.content or b""
            if len(raw) > max_bytes:
                raise ValueError(f"页面太大（{len(raw)} 字节 > {max_bytes}）")
            if "pdf" in ctype or current.lower().split("?")[0].endswith(".pdf"):
                return raw, ctype or "application/pdf", str(resp.url)
            # 用 resp.text 而不是 raw.decode()：编码由 httpx 按 header/meta 判定，
            # 中文站常见的 gb2312 漏判会直接变成一片乱码。
            return resp.text, ctype, str(resp.url)
        raise httpx.TooManyRedirects(f"跳转超过 {MAX_REDIRECTS} 次")


def _pdf_to_text(raw: bytes) -> tuple[str, str]:
    """PDF 取文字。返回 (text, error)。"""
    try:
        import pymupdf  # type: ignore
    except ImportError:
        try:
            import fitz as pymupdf  # type: ignore
        except ImportError:
            return "", "pdf_no_dep"
    try:
        doc = pymupdf.open(stream=raw, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        return "", f"pdf_open_failed: {type(exc).__name__}"
    pages: list[str] = []
    try:
        for page in doc:
            pages.append(page.get_text("text") or "")
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass
    return "\n".join(pages), ""


def _html_to_text(html: str) -> tuple[str, str]:
    """HTML 抽正文，返回 (title, text)。优先 trafilatura，残了就退回正则。"""
    title = ""
    try:
        from .web_search import _extract_main_text  # 复用既有实现，不重复写抽取规则

        # title 单独取：trafilatura 的 extract 不带标题，而标题对判断「这链接讲什么」很关键
        m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.IGNORECASE | re.DOTALL)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()[:_TITLE_MAX]
        text = _extract_main_text(html, max_chars=10 ** 9)
        return title, (text or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.info(f"dl_senpai link html extract failed: {type(exc).__name__}: {exc}")
        stripped = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html or "", flags=re.I | re.S)
        stripped = re.sub(r"<[^>]+>", " ", stripped)
        return title, re.sub(r"\s+", " ", stripped).strip()


async def _browser_render(url: str, cfg: Any) -> tuple[str, str]:
    """SPA 兜底：真开一次浏览器，把渲染后的 innerText 拿回来。"""
    from .browser_agent import _with_page
    from .web_login import optional_state_path, state_path

    captured: dict[str, str] = {}
    # 有 cookie 罐就带上，跑完再回写：验证是按"是不是老熟人"判的
    site = _site_of_url(url)
    storage_state = optional_state_path(site, cfg) if site else None
    persist_to = str(state_path(site, cfg)) if site else None

    budget = float(getattr(cfg, "link_read_timeout", 20.0) or 20.0)
    min_ready = int(getattr(cfg, "link_read_browser_min_chars", 400) or 400)
    # 取"最大的一块文字"：有的站正文在 article 里，有的站只有 body 带料。
    _TEXT_JS = (
        "() => {"
        " const sels = ['article','main','#content','.post-content','.rich_media_content','.content'];"
        " let best = document.body ? document.body.innerText : '';"
        " for (const s of sels) {"
        "   const el = document.querySelector(s);"
        "   if (el && el.innerText && el.innerText.length > best.length) { best = el.innerText; }"
        " }"
        " return best; }"
    )

    async def _work(page: Any) -> None:
        await page.goto(url, wait_until="domcontentloaded", timeout=25000)
        # 先等常用正文容器（有就快），再按"文字长度够不够"轮询。
        # 为什么不能固定 sleep：小黑盒这类站的正文是 XHR 回来才挂上 DOM 的，
        # 等 1.2 秒只拿得到导航和备案号（曾把页脚当正文，见 _looks_like_chrome）。
        for selector in ("article", "main", "#content", ".post-content", ".rich_media_content"):
            try:
                await page.wait_for_selector(selector, timeout=3000)
                break
            except Exception:  # noqa: BLE001
                continue
        best = ""
        deadline = time.time() + budget
        while time.time() < deadline:
            try:
                text = await page.evaluate(_TEXT_JS) or ""
            except Exception:  # noqa: BLE001 — 导航中途页面被换掉时会抛
                text = ""
            if len(text) > len(best):
                best = text
            if len(best) >= min_ready:
                break
            try:
                await page.mouse.wheel(0, 1200)  # 触发懒加载
            except Exception:  # noqa: BLE001
                pass
            try:
                await page.wait_for_timeout(700)
            except Exception:  # noqa: BLE001
                break
        captured["title"] = (await page.title()) or ""
        captured["text"] = best

    await _with_page(cfg, _work, storage_state=storage_state, persist_path=persist_to)
    text = re.sub(r"\n{3,}", "\n\n", (captured.get("text") or "")).strip()
    return (captured.get("title") or "").strip()[:_TITLE_MAX], text


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """超长按「头 + 尾」保留。

    为什么留尾巴：很多文章的关键结论/免责声明在最后，只留开头会让她的
    判断系统性偏向前半篇。
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text, False
    head = int(max_chars * 0.7)
    tail = max_chars - head
    return (
        text[:head].rstrip() + "\n\n……（中间省略）……\n\n" + text[-tail:].lstrip(),
        True,
    )


# ------------------------------------------------------------------ 摘要


async def summarize_text(text: str, *, focus: str = "", cfg: Any = None) -> str:
    """把长正文压成要点。失败就返回空串，由调用方退回截断原文。"""
    if not text.strip():
        return ""
    if cfg is not None and not bool(getattr(cfg, "link_read_summary_enable", True)):
        return ""
    try:
        from .llm import get_llm

        llm = get_llm(cfg)
        prompt = (
            "下面是一篇网页的正文。请用中文给出 3~5 条要点，每条一行、以「- 」开头，"
            "只写文章里确实有的事实和数字，不要评价、不要补充背景知识。\n"
            + (f"（对方只关心：{focus}。这些点优先写；正文里没有就直说没有。）\n" if focus else "")
            + "\n正文：\n"
            + text[:12000]
        )
        completion = await llm._complete_once(
            [{"role": "user", "content": prompt}],
            max_tokens=400,
            timeout=40.0,
        )
        raw = getattr(completion, "text", None)
        if not isinstance(raw, str):
            raw = completion if isinstance(completion, str) else ""
        return (raw or "").strip()
    except Exception as exc:  # noqa: BLE001 — 摘要只是省钱的手段，不该成为失败点
        logger.info(f"dl_senpai link summarize skipped: {type(exc).__name__}: {exc}")
        return ""


# ------------------------------------------------------------------ 缓存


def _cache_dir(cfg: Any) -> Path:
    raw = getattr(cfg, "link_read_cache_dir", "") or ""
    if raw:
        return Path(str(raw))
    base = Path(str(getattr(cfg, "memory_dir", "data/dl_senpai/memory")))
    return base.parent / "link_cache"


def _cache_key(url: str) -> str:
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:20]


def cache_get(url: str, cfg: Any) -> LinkDoc | None:
    ttl = float(getattr(cfg, "link_read_cache_ttl_sec", 21600) or 0)
    if ttl <= 0:
        return None
    path = _cache_dir(cfg) / f"{_cache_key(url)}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if time.time() - float(data.get("at") or 0) > ttl:
        return None
    doc = LinkDoc(
        url=str(data.get("url") or url),
        final_url=str(data.get("final_url") or ""),
        title=str(data.get("title") or ""),
        text=str(data.get("text") or ""),
        source=str(data.get("source") or ""),
        truncated=bool(data.get("truncated")),
        total_chars=int(data.get("total_chars") or 0),
        note=str(data.get("note") or ""),
    )
    # 摘要也要进缓存：否则「第二次读同一篇」会退化成没有要点的原文，
    # 表现就是同一链接前后两次回答详细程度不一样。
    summary = str(data.get("summary") or "")
    if summary:
        doc.extra["summary"] = summary
    # 缓存里也可能躺着「只有站名」或「只剩页脚」的残次品（旧版本存进去的），
    # 一并当未命中——宁可重抓一次，也不能把假成功当结果给她。
    if not doc.ok or len(doc.text.strip()) < _MIN_USABLE_CHARS:
        return None
    if _looks_like_chrome(doc.text or ""):
        return None
    doc.cached = True
    return doc


def cache_put(doc: LinkDoc, cfg: Any) -> None:
    if not doc.ok:
        return
    path = _cache_dir(cfg) / f"{_cache_key(doc.url)}.json"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "url": doc.url,
                    "final_url": doc.final_url,
                    "title": doc.title,
                    "text": doc.text,
                    "source": doc.source,
                    "truncated": doc.truncated,
                    "total_chars": doc.total_chars,
                    "note": doc.note,
                    # 摘要一起存：缓存命中时也要有要点，不能只剩原文
                    "summary": str(doc.extra.get("summary") or ""),
                    "at": time.time(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug(f"dl_senpai link cache write failed: {exc}")


# ------------------------------------------------------------------ 主入口


async def read_link(url: str, *, cfg: Any = None, focus: str = "") -> LinkDoc:
    """读一个链接。任何失败都变成 LinkDoc.error，不抛异常。

    为什么不让它抛：它挂在每条消息的入口上（群里谁贴链接都会走一遍），
    一次超时不该让整条消息处理链断掉；同时失败原因必须能传出去，
    否则她只能回「我读不了」，说不出为什么。
    """
    target = (url or "").strip()
    doc = LinkDoc(url=target)
    if not target:
        doc.error = "empty_url"
        return doc

    cached = cache_get(target, cfg)
    if cached is not None:
        return cached

    max_chars = int(getattr(cfg, "link_read_max_chars", 6000) or 6000)
    try:
        payload, ctype, final_url = await _follow_http(target, cfg)
    except PermissionError as exc:
        doc.error = f"blocked: {exc}"
        return doc
    except httpx.TimeoutException:
        doc.error = "timeout"
        return doc
    except httpx.TooManyRedirects as exc:
        doc.error = f"redirect_loop: {exc}"
        return doc
    except httpx.HTTPStatusError as exc:
        code = getattr(getattr(exc, "response", None), "status_code", 0)
        doc.error = f"http_{code}" if code else "http_error"
        return doc
    except ValueError as exc:
        doc.error = f"too_large_or_bad: {exc}"
        return doc
    except Exception as exc:  # noqa: BLE001
        doc.error = f"fetch_failed: {type(exc).__name__}"
        doc.note = str(exc)[:160]
        return doc

    doc.final_url = final_url
    if isinstance(payload, bytes):
        text, pdf_err = await asyncio.to_thread(_pdf_to_text, payload)
        doc.source = "pdf"
        if pdf_err:
            doc.error = pdf_err
            return doc
        title, body = "", text
    elif "html" in ctype or payload.lstrip()[:15].lower().startswith(("<!doctype", "<html")):
        title, body = _html_to_text(payload)
        doc.source = "http"
    else:
        title, body = "", payload
        doc.source = "plain"

    body = (body or "").strip()
    # 静态 HTML 抽不到东西 → 大概率是 SPA 空壳。上浏览器再来一次。
    if len(body) < _EMPTY_TEXT_THRESHOLD and bool(getattr(cfg, "link_read_use_browser", True)):
        try:
            b_title, b_text = await _browser_render(final_url or target, cfg)
            if len(b_text) > len(body):
                title, body = b_title or title, b_text
                doc.source = "browser"
        except Exception as exc:  # noqa: BLE001 — 浏览器失败就用手上这点
            logger.info(f"dl_senpai link browser fallback failed: {type(exc).__name__}: {exc}")

    doc.title = title
    doc.total_chars = len(body)
    # 只剩站名（比如 SPA 空壳只剩 <title>）时不算读到了：
    # 让她把「小黑盒 - 玩家高能聚集地」当成正文去回答，比直接说读不到更糟。
    if len(body) < _MIN_USABLE_CHARS or _looks_like_chrome(body):
        doc.error = "empty_text"
        doc.note = f"content-type={ctype} 正文仅 {len(body)} 字"
        return doc

    summary = ""
    threshold = int(getattr(cfg, "link_read_summary_min_chars", 4000) or 0)
    if threshold > 0 and len(body) > threshold:
        summary = await summarize_text(body, focus=focus, cfg=cfg)
        if summary:
            doc.extra["summary"] = summary

    doc.text, doc.truncated = _truncate(body, max_chars)
    cache_put(doc, cfg)
    return doc


def render_brief(doc: LinkDoc, *, focus: str = "") -> str:
    """把 LinkDoc 拼成一段可以直接塞进提示词的文本。

    格式刻意固定：模型看到「【系统读到的链接正文】」就知道这不是对方说的，
    而是它自己上号读到的，别当成对方在引用。
    """
    if not doc.ok:
        reason = {
            "timeout": "超时（对方可能发了需要登录或极慢的站）",
            "empty_text": "页面里没抽到正文（可能是纯图片页、要登录，或者站点这次弹了人机验证）",
            "pdf_no_dep": "PDF 支持没装（pip install pymupdf）",
        }.get(doc.error.split(":")[0], doc.error or "未知原因")
        return (
            f"【系统读链接失败】{doc.url}\n原因：{reason}\n"
            "（读不到就当没读到，直接说「这链接我打不开」，不要凭 URL 猜内容。）"
        )

    lines = [f"【系统读到的链接正文】{doc.url}"]
    if doc.final_url and doc.final_url != doc.url:
        lines.append(f"实际地址：{doc.final_url}")
    if doc.title:
        lines.append(f"标题：{doc.title}")
    lines.append(f"正文约 {doc.total_chars} 字，来源：{doc.source}" + ("（走缓存）" if doc.cached else ""))
    summary = str(doc.extra.get("summary") or "")
    if summary:
        lines.append("【要点】（原文太长，以下是系统先压过的要点，细节可据此回答）")
        lines.append(summary)
        lines.append("【原文片段】")
    lines.append(doc.text)
    if doc.truncated:
        lines.append("（正文已截断，只给了首尾。别说「全文看完了」。）")
    if focus:
        lines.append(f"（对方关心的是：{focus}。只答这个，别复述全文。）")
    return "\n".join(lines)


async def auto_read_brief(text: str, cfg: Any = None) -> str:
    """主动读：消息里出现链接就先读一遍，返回可注入提示词的文本。

    只读第一个链接：一条消息贴五个链接通常是刷屏或转载，全读既慢又吵。
    """
    if cfg is None:
        return ""
    if not bool(getattr(cfg, "link_read_enable", True)):
        return ""
    if not bool(getattr(cfg, "link_read_auto", True)):
        return ""
    url = extract_first_url(text)
    if not url:
        return ""
    doc = await read_link(url, cfg=cfg, focus=(text or "")[:200])
    if doc.ok:
        logger.info(f"dl_senpai auto link read ok url={url} chars={doc.total_chars} src={doc.source}")
    else:
        logger.info(f"dl_senpai auto link read failed url={url} err={doc.error}")
    return render_brief(doc, focus=(text or "")[:200])
