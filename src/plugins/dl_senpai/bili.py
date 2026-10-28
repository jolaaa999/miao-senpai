"""B站只读：热门、搜视频、看单个视频 + 热评。

为什么走接口而不是浏览器
------------------------
实测这四个口匿名就能拿（search/type、view、reply、popular 都 code=0），
只有 ranking/v2 会被风控挡（code=-352），所以「热门」改用
web-interface/popular。少开一次 chromium，快 5~10 秒，也不会因为
页面改版就全废。

为什么要先 warmup
-----------------
直接打 API 拿不到 buvid3，B 站会对无 cookie 的请求回 -352。先访问一次
www.bilibili.com 收下 cookie 再调，稳定得多。cookie 缓存 6 小时。

读与写
------
匿名能读的部分（热门/搜索/单个视频+热评）不需要登录态。
需要登录态的两件事另说：拉「回复我的」（x/msgfeed/reply）和发评论
（x/v2/reply/add，要 SESSDATA + bili_jct）。这两件事**只对登录的小号生效**，
登录态存在 data/dl_senpai/web_login/bili.json，由 scripts\\login-web.py bili 写入。

为什么写评论不用浏览器：B 站有公开写接口，带上 cookie 和 csrf 就能发，
不像小黑盒要页面签名。代价是风控更看账号行为，所以写之前一律过
write_guard 的节奏护栏（间隔 / 同帖 / 文案去重）。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

__all__ = [
    "bili_play_url",
    "bili_watch_report",
    "bili_feed",
    "BiliError",
    "BiliMessage",
    "BiliVideo",
    "bili_comment",
    "bili_favorite",
    "bili_hot",
    "bili_like",
    "bili_messages",
    "bili_oid_of",
    "bili_whoami",
    "bili_search",
    "bili_transcript",
    "bili_video",
    "default_fav_folder",
    "extract_bvid",
    "render_hot",
    "render_messages",
    "render_search",
    "pick_subtitle",
    "render_video",
    "transcript_from_cues",
]

_API = "https://api.bilibili.com"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_HEADERS = {
    "User-Agent": _UA,
    "Referer": "https://www.bilibili.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# BV 号是 BV + 10 位 base58 字符；av 号是老格式，接口都还认
_BV_RE = re.compile(r"(BV[0-9A-Za-z]{10})")
_AV_RE = re.compile(r"av(\d+)", re.IGNORECASE)
_SHORT_RE = re.compile(r"https?://b23\.tv/\S+", re.IGNORECASE)
_EM_RE = re.compile(r"</?em[^>]*>")

_COOKIE_TTL = 6 * 3600.0
_cookie_lock = asyncio.Lock()
_cookies: dict[str, str] = {}
_warmed_at = 0.0


class BiliError(Exception):
    """B站接口不可用。message 里放人话，工具层直接回给模型。"""


@dataclass
class BiliVideo:
    """一个视频的最小信息集——够她聊天用，不多抓。"""

    bvid: str = ""
    aid: int = 0
    title: str = ""
    author: str = ""
    desc: str = ""
    duration: str = ""
    play: int = 0
    danmaku: int = 0
    like: int = 0
    pubdate: int = 0
    url: str = ""
    # 热评：(昵称, 内容, 点赞数)
    comments: list[tuple[str, str, int]] = field(default_factory=list)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _fmt_count(num: int) -> str:
    """播放量这类大数转成"12.3万"，她说话时不会念出一长串数字。"""
    if num >= 100_000_000:
        return f"{num / 100_000_000:.1f}亿"
    if num >= 10_000:
        return f"{num / 10_000:.1f}万"
    return str(num)


def _fmt_duration(seconds: Any) -> str:
    total = _as_int(seconds)
    if total <= 0:
        return ""
    return f"{total // 60:02d}:{total % 60:02d}"


def _fmt_date(ts: Any) -> str:
    stamp = _as_int(ts)
    if stamp <= 0:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(stamp))


def _strip_em(text: str) -> str:
    """搜索结果带 <em> 高亮标签，简介里还夹着 &quot; 这类实体，一并还原。

    不还原的话她会照着念「说&quot;今天不想看&quot;」——很难看，也费 token。
    """
    import html

    return html.unescape(_EM_RE.sub("", text or "")).strip()


async def _ensure_cookies() -> dict[str, str]:
    """访问一次首页拿 buvid3（缓 6 小时）。"""
    global _warmed_at
    async with _cookie_lock:
        if _cookies and (time.time() - _warmed_at) < _COOKIE_TTL:
            return dict(_cookies)
        try:
            async with httpx.AsyncClient(
                headers=_HEADERS, timeout=10.0, follow_redirects=True
            ) as client:
                await client.get("https://www.bilibili.com/")
                _cookies.clear()
                _cookies.update({k: v for k, v in client.cookies.items()})
                _warmed_at = time.time()
        except Exception:  # noqa: BLE001 — 拿不到 cookie 也照试，多数接口还能用
            _cookies.clear()
            _warmed_at = time.time()
        return dict(_cookies)


async def _get(
    path: str, params: dict[str, Any], *, timeout: float, cookies: dict[str, str] | None = None
) -> dict[str, Any]:
    """调一个接口并检查 code。风控（-352）时换一份新 cookie 重试一次。

    cookies 参数是给「登录才有内容」的接口留的：player/v2 的字幕列表匿名一律
    返回空数组，带上 SESSDATA 才有。
    """
    for attempt in (0, 1):
        cookies = {**(await _ensure_cookies()), **(cookies or {})}
        try:
            async with httpx.AsyncClient(
                headers=_HEADERS,
                cookies=cookies or None,
                timeout=timeout,
                follow_redirects=True,
            ) as client:
                resp = await client.get(f"{_API}{path}", params=params)
                resp.raise_for_status()
                payload = resp.json()
        except httpx.HTTPError as exc:
            raise BiliError(f"B站接口连不上（{type(exc).__name__}）") from exc
        except ValueError as exc:
            raise BiliError("B站接口返回的不是 JSON") from exc

        code = _as_int(payload.get("code"))
        if code == 0:
            data = payload.get("data")
            return data if isinstance(data, dict) else {"data": data}
        if code == -352 and attempt == 0:
            # 风控：丢掉旧 cookie 重新 warmup 再来一次
            global _warmed_at
            _warmed_at = 0.0
            continue
        raise BiliError(
            f"B站接口返回 code={code} {str(payload.get('message') or '').strip()}".strip()
        )
    raise BiliError("B站接口被风控挡了（-352），过一会儿再试")


def extract_bvid(text: str) -> str:
    """从一句话/链接里认出 BV 号或 av 号。

    支持：裸 BV 号、视频页链接、av 号。b23.tv 短链需要联网解析，
    见 resolve_short_link（异步，单独一步）。
    """
    raw = (text or "").strip()
    match = _BV_RE.search(raw)
    if match:
        return match.group(1)
    match = _AV_RE.search(raw)
    if match:
        return f"av{match.group(1)}"
    return ""


async def resolve_short_link(url: str, *, cfg: Any = None) -> str:
    """把 b23.tv 短链跟到最后，取里面的 BV 号。"""
    timeout = float(getattr(cfg, "bili_timeout", 15.0) or 15.0)
    try:
        async with httpx.AsyncClient(
            headers=_HEADERS, timeout=timeout, follow_redirects=True
        ) as client:
            resp = await client.head(url)
            final = str(resp.url)
    except httpx.HTTPError as exc:
        raise BiliError(f"短链解析失败（{type(exc).__name__}）") from exc
    found = extract_bvid(final)
    if not found:
        raise BiliError("短链里没有 BV 号")
    return found


def _video_from_search(row: dict[str, Any]) -> BiliVideo:
    bvid = str(row.get("bvid") or "")
    return BiliVideo(
        bvid=bvid,
        aid=_as_int(row.get("aid")),
        title=_strip_em(str(row.get("title") or "")),
        author=str(row.get("author") or ""),
        desc=_strip_em(str(row.get("description") or "")),
        duration=_fmt_duration(row.get("duration")),
        play=_as_int(row.get("play")),
        danmaku=_as_int(row.get("video_review") or row.get("danmaku")),
        like=_as_int(row.get("like")),
        pubdate=_as_int(row.get("pubdate")),
        url=f"https://www.bilibili.com/video/{bvid}" if bvid else "",
    )


def _video_from_popular(row: dict[str, Any]) -> BiliVideo:
    stat = row.get("stat") or {}
    owner = row.get("owner") or {}
    bvid = str(row.get("bvid") or "")
    return BiliVideo(
        bvid=bvid,
        aid=_as_int(row.get("aid")),
        title=str(row.get("title") or ""),
        author=str(owner.get("name") or ""),
        desc=str(row.get("desc") or ""),
        duration=_fmt_duration(row.get("duration")),
        play=_as_int(stat.get("view")),
        danmaku=_as_int(stat.get("danmaku")),
        like=_as_int(stat.get("like")),
        pubdate=_as_int(row.get("pubdate")),
        url=f"https://www.bilibili.com/video/{bvid}" if bvid else "",
    )


def _video_from_rcmd(row: dict[str, Any]) -> BiliVideo:
    """首页推荐流的一条。字段比 popular 少，但该有的都有。"""
    stat = row.get("stat") or {}
    owner = row.get("owner") or {}
    bvid = str(row.get("bvid") or "")
    return BiliVideo(
        bvid=bvid,
        aid=_as_int(row.get("id")),
        title=str(row.get("title") or ""),
        author=str(owner.get("name") or ""),
        desc=str(row.get("desc") or ""),
        duration=_fmt_duration(row.get("duration")),
        play=_as_int(stat.get("view")),
        danmaku=_as_int(stat.get("danmaku")),
        like=_as_int(stat.get("like")),
        pubdate=_as_int(row.get("pubdate")),
        url=f"https://www.bilibili.com/video/{bvid}" if bvid else "",
    )


async def bili_feed(limit: int = 12, *, cfg: Any = None) -> tuple[list[BiliVideo], str]:
    """「刷新一次首页」——每轮都拿新的一批，不是把同一页反复念一遍。

    主人 2026-10-27 的要求：每刷完一个视频就刷新一下页面，不然会一直刷到同一个。
    原来 popular?pn=1 永远返回同一批，热榜几小时不动，她就把同一个 BV 点了 7 次。

    这里优先走首页推荐流（wbi/index/top/feed/rcmd）：实测连叫两次重合 0 条，
    就是「刷新页面」该有的效果；它带 wbi 前缀，但匿名实测能用（不带 w_rid 也回 200）。
    推荐流挂了就退回热门**随机页**（pn 1~5，实测页间重合也是 0）。

    返回 (视频列表, 来源)，来源进台账，出问题能一眼看出走的是哪条路。
    """
    timeout = float(getattr(cfg, "bili_timeout", 15.0) or 15.0)
    size = max(1, min(int(limit), 30))

    # fresh_idx 决定推哪一批，随机化就等于「往下滑了几屏」
    try:
        data = await _get(
            "/x/web-interface/wbi/index/top/feed/rcmd",
            {"ps": size, "fresh_type": 4, "fresh_idx": random.randint(1, 6)},
            timeout=timeout,
        )
        items = [r for r in (data.get("item") or []) if isinstance(r, dict)]
        videos = [_video_from_rcmd(r) for r in items]
        videos = [v for v in videos if v.bvid.startswith("BV")]
        if len(videos) >= 3:
            return videos[:size], "rcmd"
    except BiliError:
        pass

    # 退路：热门随机页。固定 pn=1 就是「重复刷同一个」的根源，所以这里必须随机
    data = await _get(
        "/x/web-interface/popular",
        {"ps": size, "pn": random.randint(1, 5)},
        timeout=timeout,
    )
    rows = data.get("list") or []
    return [_video_from_popular(r) for r in rows if isinstance(r, dict)][:size], "popular"


async def bili_hot(limit: int = 10, *, cfg: Any = None) -> list[BiliVideo]:
    """综合热门（保留旧名字，内部走 bili_feed，每叫一次都是新的一批）。"""
    videos, _source = await bili_feed(limit=limit, cfg=cfg)
    return videos


async def bili_search(keyword: str, limit: int = 8, *, cfg: Any = None) -> list[BiliVideo]:
    """搜视频。search/type 匿名可用，返回里带 bvid/作者/播放。"""
    query = (keyword or "").strip()
    if not query:
        raise BiliError("搜索词是空的")
    timeout = float(getattr(cfg, "bili_timeout", 15.0) or 15.0)
    data = await _get(
        "/x/web-interface/search/type",
        {"search_type": "video", "keyword": query, "page": 1},
        timeout=timeout,
    )
    rows = data.get("result") or []
    items = [_video_from_search(r) for r in rows if isinstance(r, dict)]
    return items[:limit]


_SUB_PREF = ("zh-Hans", "zh-CN", "zh", "ai-zh", "ai-en")


def pick_subtitle(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """从 player/v2 的 subtitles 里挑一条。挑不到返回空 dict。

    为什么先人工后 AI：ai-zh 是机器听的，实测会把口播听成「投降 包村 拥 威信 扫」
    这种幻觉词。她要是拿这个去写日记，写出来的是别人的错字。
    人工字幕（zh-Hans/zh-CN）先挑，实在没有才退回 AI 的。
    """
    usable = [r for r in rows if isinstance(r, dict) and r.get("subtitle_url")]
    for lan in _SUB_PREF:
        for row in usable:
            if str(row.get("lan") or "") == lan:
                return row
    return usable[0] if usable else {}


def _ts(seconds: float) -> str:
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


def transcript_from_cues(cues: list[dict[str, Any]], *, max_chars: int = 2000) -> str:
    """把逐句字幕拼成带时间点的文字稿。

    为什么不一句一行：B 站字幕一句常常只有四五个字，一句一行会变成几百行，
    token 全花在换行和时间戳上。这里按 15 秒一桶合并，读起来像正常的说话段落，
    又保留了「大概在第几分钟」的定位感。
    """
    buckets: dict[int, list[str]] = {}
    for cue in cues:
        if not isinstance(cue, dict):
            continue
        text = " ".join(str(cue.get("content") or "").split())
        if not text:
            continue
        try:
            start = float(cue.get("from") or 0)
        except (TypeError, ValueError):
            start = 0.0
        buckets.setdefault(int(start // 15), []).append(text)
    if not buckets:
        return ""
    lines: list[str] = []
    used = 0
    for key in sorted(buckets):
        line = f"[{_ts(key * 15)}] " + " ".join(buckets[key])
        if used + len(line) > max_chars:
            lines.append("…（后面还长，先到这儿）")
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines)


async def _fetch_subtitle_body(
    url: str, *, timeout: float, cfg: Any = None
) -> list[dict[str, Any]]:
    """字幕文件在另一个域名上，返回的也不是 {code,data} 那套。"""
    if url.startswith("//"):
        url = "https:" + url
    cookies = {}
    try:
        cookies = _state_cookies(cfg)
    except Exception:  # noqa: BLE001 — 字幕匿名也能读，别为了登录态把这条路堵死
        cookies = {}
    merged = {**await _ensure_cookies(), **(cookies or {})}
    try:
        async with httpx.AsyncClient(
            headers={**_HEADERS, "Referer": "https://www.bilibili.com/"}, cookies=merged or None,
            timeout=timeout, follow_redirects=True,
        ) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPError as exc:
        raise BiliError(f"字幕文件下不下来（{type(exc).__name__}）") from exc
    except ValueError as exc:
        raise BiliError("字幕文件不是 JSON") from exc
    body = payload.get("body") if isinstance(payload, dict) else None
    return body if isinstance(body, list) else []


async def bili_transcript(
    ref: str, *, cfg: Any = None, max_chars: int = 2000
) -> str:
    """拿一个视频的字幕文字稿；没有字幕就返回空串（这不是错误）。

    她看不了画面（模型是纯文本），但能读字幕。有了这段，日记/记忆/评论才可能
    说到视频里真讲了什么，而不是只能复述标题。限制也说清楚：冷门、纯音乐、
    刚发布的视频常常没字幕，AI 字幕还有错字，所以不能逐字引用。
    """
    timeout = float(getattr(cfg, "bili_timeout", 15.0) or 15.0)
    target = extract_bvid(ref)
    if not target:
        if _SHORT_RE.search(ref or ""):
            target = await resolve_short_link(ref, cfg=cfg)
        else:
            raise BiliError("没认出 BV 号或 av 号")
    params = {"bvid": target} if target.startswith("BV") else {"aid": target[2:]}
    view = await _get("/x/web-interface/view", params, timeout=timeout)
    bvid = str(view.get("bvid") or target)
    cid = _as_int(view.get("cid"))
    if not cid:
        # 分P视频的 cid 在 pages 里；单P视频顶层就有
        pages = view.get("pages") or []
        if pages and isinstance(pages[0], dict):
            cid = _as_int(pages[0].get("cid"))
    if not cid:
        raise BiliError("这个视频拿不到 cid，读不了字幕")
    # 字幕列表要登录态才给：匿名的 player/v2 里 subtitles 恒为空数组
    try:
        login = _state_cookies(cfg)
    except BiliError:
        login = {}
    player = await _get(
        "/x/player/v2", {"bvid": bvid, "cid": cid}, timeout=timeout, cookies=login
    )
    # 为什么要取两次再比对：2026-10-27 实测，同一个 cid、同一个字幕 id，
    # 连着问三次拿到的是**三段完全不同的内容**（一次讲珍妮·古道尔、一次是视频本身、
    # 一次在讲"我的兴趣爱好"）。B 站那边的 AI 字幕对象会串，路径 hash 每次都变。
    # 她拿错字幕去写日记，会写出"字幕讲的是锅包肉，跟标题对不上"这种废条目。
    # 所以：两次一致才认，不一致就当没字幕——宁可不给，也不给错的。
    previous = ""
    for attempt in range(3):
        rows = ((player.get("subtitle") or {}).get("subtitles")) or []
        chosen = pick_subtitle(rows if isinstance(rows, list) else [])
        if not chosen:
            if attempt == 0:
                return ""
        else:
            cues = await _fetch_subtitle_body(
                str(chosen.get("subtitle_url") or ""), timeout=timeout, cfg=cfg
            )
            text = transcript_from_cues(cues, max_chars=max_chars)
            # 比对用首句：同一个视频的两份字幕，开头一定一致；串台的一定不一致
            fingerprint = (text or "")[:60]
            if fingerprint and fingerprint == previous:
                return text
            previous = fingerprint
        if attempt == 2:
            break
        # 重新问一次 player/v2 换一份字幕对象，别复用刚才那个 URL
        player = await _get(
            "/x/player/v2", {"bvid": bvid, "cid": cid}, timeout=timeout, cookies=login
        )
    return ""


# 写接口返回的错误码翻译。点赞/收藏共用一套，共通的放这里，各自特殊的另加。
_WRITE_HINTS: dict[int, str] = {
    -101: "登录态失效了，重跑 login-web.py bili",
    -111: "csrf 校验失败（bili_jct 过期），重跑一次 login-web.py bili",
    -400: "参数不对",
    -403: "没有权限做这个操作",
    -404: "找不到这个视频",
    -412: "被 B 站风控挡了，先停一会儿",
    -509: "操作太频繁，过一会儿再说",
    -702: "账号被限制操作",
}


async def _post(
    path: str,
    payload: dict[str, Any],
    *,
    cfg: Any = None,
    timeout: float = 25.0,
    referer: str = "https://www.bilibili.com/",
    action: str = "这个操作",
    extra_hints: dict[int, str] | None = None,
) -> dict[str, Any]:
    """带登录态的写请求。统一塞 csrf、统一翻错误码。

    为什么统一成一个口子：点赞、收藏、以后可能加的关注/投币，全都走
    "cookie + bili_jct + POST data" 这一套；分开写会到处复制错误码翻译，
    最后每个接口的错误信息还不一样。
    """
    cookies = _state_cookies(cfg)
    csrf = cookies.get("bili_jct") or ""
    if not csrf:
        raise BiliError("登录态里没有 bili_jct（csrf），重跑一次 login-web.py bili")
    data = {**payload, "csrf": csrf}
    try:
        async with httpx.AsyncClient(
            headers={**_HEADERS, "Referer": referer},
            cookies=cookies,
            timeout=timeout,
            follow_redirects=True,
        ) as client:
            resp = await client.post(f"{_API}{path}", data=data)
            body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise BiliError(f"{action}的请求没打通（{type(exc).__name__}）") from exc
    code = _as_int(body.get("code"))
    if code == 0:
        data_out = body.get("data")
        return data_out if isinstance(data_out, dict) else {"data": data_out}
    hints = {**_WRITE_HINTS, **(extra_hints or {})}
    hint = hints.get(code) or str(body.get("message") or "").strip() or "未知错误"
    raise BiliError(f"B站没接受{action}：code={code} {hint}")


async def bili_play_url(ref: str, *, cfg: Any = None) -> dict[str, Any]:
    """拿视频的播放直链，给抽帧用。返回 {url, duration, bvid, cid}。

    为什么只要 360p（qn=32）：抽帧看的是「画面在干什么」，不是画质。
    码率越小，ffmpeg 按时间点 seek 越快（实测 4 分钟的视频每帧 0.13~0.22 秒）。
    """
    timeout = float(getattr(cfg, "bili_timeout", 15.0) or 15.0)
    target = extract_bvid(ref)
    if not target:
        raise BiliError("没认出 BV 号")
    view = await _get("/x/web-interface/view", {"bvid": target}, timeout=timeout)
    bvid = str(view.get("bvid") or target)
    cid = _as_int(view.get("cid"))
    if not cid:
        pages = view.get("pages") or []
        if pages and isinstance(pages[0], dict):
            cid = _as_int(pages[0].get("cid"))
    if not cid:
        raise BiliError("这个视频拿不到 cid，抽不了帧")
    cookies = _state_cookies(cfg)
    play = await _get(
        "/x/player/playurl",
        {"bvid": bvid, "cid": cid, "qn": 32, "fnval": 1, "fnver": 0, "fourk": 1},
        timeout=timeout,
        cookies=cookies,
    )
    url = ""
    durl = play.get("durl") or []
    if durl and isinstance(durl[0], dict):
        url = str(durl[0].get("url") or "")
    if not url:
        # 有些视频只给 dash（音视频分离），取最低清的那路视频流就够抽帧
        videos = ((play.get("dash") or {}).get("video") or [])
        if videos and isinstance(videos[0], dict):
            url = str(videos[0].get("baseUrl") or videos[0].get("base_url") or "")
    if not url:
        raise BiliError("这个视频没有可用的播放地址（可能是付费/番剧）")
    return {
        "url": url,
        "duration": _as_int(view.get("duration")),
        "bvid": bvid,
        "cid": cid,
        "title": str(view.get("title") or ""),
    }

async def bili_watch_report(
    ref: str, *, cfg: Any = None, seconds: int = 0, verify: bool = True
) -> dict[str, Any]:
    """替她「看」一遍：起播上报 + 播放心跳。

    为什么要做这件事：B 站的观看历史只认**播放器上报**。她原来那条链路全是
    view / player/v2 / 字幕 JSON，平台根本不知道她"看过"，所以主人翻历史一条都看不到；
    顺带一个后果是观看时长不算经验——她卡在 Lv1 175/200 上不去，评论功能就一直关着。

    实测（2026-10-27 20:48）：click/web/h5 + web/heartbeat 两个报告发完，3 秒后
    历史接口第一条就是这条视频，progress 180，和网页播放器留下的痕迹一样。

    seconds=0 表示按视频时长随机取（60 秒起，最多 5 分钟）——一律播满会显得机械。
    """
    timeout = float(getattr(cfg, "bili_timeout", 15.0) or 15.0)
    target = extract_bvid(ref)
    if not target:
        raise BiliError("没认出 BV 号")
    view = await _get("/x/web-interface/view", {"bvid": target}, timeout=timeout)
    bvid = str(view.get("bvid") or target)
    aid = _as_int(view.get("aid"))
    cid = _as_int(view.get("cid"))
    if not aid or not cid:
        pages = view.get("pages") or []
        if pages and isinstance(pages[0], dict):
            cid = cid or _as_int(pages[0].get("cid"))
    if not aid or not cid:
        raise BiliError("这个视频拿不到 aid/cid，报不了播放")

    cookies = _state_cookies(cfg)
    mid = str(cookies.get("DedeUserID") or "")
    if not mid:
        raise BiliError("登录态里没有 DedeUserID，重跑一次 login-web.py bili")

    # 播多久：看着像真的看了一段，又不至于每次都同一个数
    duration = _as_int(view.get("duration"))
    played = int(seconds or 0)
    if played <= 0:
        ceiling = max(60, min(duration or 300, 300))
        played = random.randint(60, ceiling)
    played = max(5, min(played, max(duration, 5)))

    now = int(time.time())
    common = {"aid": aid, "cid": cid, "mid": mid, "bvid": bvid, "type": 3, "sub_type": 0}
    await _post(
        "/x/click-interface/click/web/h5",
        {**common, "part": 1, "lv": 0, "stime": now, "jsonp": "jsonp"},
        cfg=cfg,
        timeout=timeout,
        referer=f"https://www.bilibili.com/video/{bvid}/",
        action="上报起播",
    )
    # 起播和心跳之间隔一下：网页播放器也是播了一两秒才发心跳，同一时刻连发两个不像人
    await asyncio.sleep(1.2)
    await _post(
        "/x/click-interface/web/heartbeat",
        {
            **common,
            "played_time": played,
            "real_played_time": played,
            "realtime": played,
            "start_ts": now - played,
            "dt": 2,
            "play_type": 0,
            "last_play_progress_time": played,
            "max_play_progress_time": played,
            "extra": json.dumps({"player_version": "4.9.0", "video_attribute": 0}),
        },
        cfg=cfg,
        timeout=timeout,
        referer=f"https://www.bilibili.com/video/{bvid}/",
        action="上报播放进度",
    )

    result: dict[str, Any] = {"ok": True, "bvid": bvid, "played": played}
    if verify:
        # 上报成功不等于历史里真有，所以回头查一眼。查不到也别报错，只是标出来。
        try:
            hist = await _get(
                "/x/web-interface/history/cursor",
                {"ps": 10, "type": "archive"},
                timeout=timeout,
                cookies=cookies,
            )
            rows = hist.get("list") or []
            result["in_history"] = any(
                str((row.get("history") or {}).get("bvid") or "") == bvid
                for row in rows
                if isinstance(row, dict)
            )
        except BiliError:
            result["in_history"] = None
    return result

async def bili_like(ref: str, *, cfg: Any = None, undo: bool = False) -> dict[str, Any]:
    """给视频点赞（undo=True 就是取消赞）。

    点赞是"表态"不是"评论"，所以不需要 Lv2，也不走 write_guard 的文案护栏；
    但它一样是账号行为，调用方仍然要控频率（刷太勤一样会被限）。
    """
    timeout = float(getattr(cfg, "bili_timeout", 15.0) or 15.0)
    aid, bvid = await bili_oid_of(ref, cfg=cfg)
    if not aid:
        raise BiliError("没拿到视频 aid，点不了赞")
    await _post(
        "/x/web-interface/archive/like",
        {"aid": aid, "like": 2 if undo else 1},
        cfg=cfg,
        timeout=timeout,
        referer=f"https://www.bilibili.com/video/{bvid or 'av' + str(aid)}",
        action="取消赞" if undo else "点赞",
        extra_hints={
            # 实测常见：65004=已经赞过再点会失败，65006=取消赞时本来没赞
            65004: "这条已经赞过了（或者刚被取消），状态没变",
            65006: "取消赞时它本来就没被赞",
        },
    )
    return {"ok": True, "aid": aid, "bvid": bvid, "liked": not undo}


async def default_fav_folder(*, cfg: Any = None) -> int:
    """取「默认收藏夹」的 id。收藏接口必须指定文件夹，没有就用默认的。"""
    who = await bili_whoami(cfg=cfg)
    mid = str(who.get("mid") or "")
    if not mid:
        raise BiliError("拿不到自己的 mid，找不到收藏夹")
    data = await _get(
        "/x/v3/fav/folder/created/list-all",
        {"up_mid": mid},
        timeout=float(getattr(cfg, "bili_timeout", 15.0) or 15.0),
        cookies=_state_cookies(cfg),
    )
    rows = data.get("list") if isinstance(data, dict) else None
    folders = [r for r in (rows or []) if isinstance(r, dict)]
    if not folders:
        # 默认收藏夹不存在的情况极少，但真没有就只能报出来，不能瞎猜 id
        raise BiliError("这号没有可用的收藏夹（接口没返回任何文件夹）")
    for row in folders:
        if _as_int(row.get("id")) and str(row.get("title") or "") in ("默认收藏夹", "默认收藏"):
            return _as_int(row.get("id"))
    return _as_int(folders[0].get("id"))


async def bili_favorite(
    ref: str, *, cfg: Any = None, folder_id: int = 0, undo: bool = False
) -> dict[str, Any]:
    """收藏视频（undo=True 取消收藏）。folder_id=0 时用配置或默认收藏夹。"""
    timeout = float(getattr(cfg, "bili_timeout", 15.0) or 15.0)
    aid, bvid = await bili_oid_of(ref, cfg=cfg)
    if not aid:
        raise BiliError("没拿到视频 aid，收藏不了")
    fid = int(folder_id or getattr(cfg, "bili_fav_folder_id", 0) or 0)
    if not fid:
        fid = await default_fav_folder(cfg=cfg)
    payload: dict[str, Any] = {"rid": aid, "type": 2}
    if undo:
        payload["del_media_ids"] = str(fid)
    else:
        payload["add_media_ids"] = str(fid)
    await _post(
        "/x/v3/fav/resource/deal",
        payload,
        cfg=cfg,
        timeout=timeout,
        referer=f"https://www.bilibili.com/video/{bvid or 'av' + str(aid)}",
        action="取消收藏" if undo else "收藏",
        extra_hints={
            -403: "收藏夹满了或者没权限",
            11201: "这条已经在收藏夹里了",
        },
    )
    return {"ok": True, "aid": aid, "bvid": bvid, "folder_id": fid, "favored": not undo}


async def bili_video(ref: str, *, comment_limit: int = 5, cfg: Any = None) -> BiliVideo:
    """看一个视频：标题/UP/简介/播放/时长，外加几条热评。"""
    timeout = float(getattr(cfg, "bili_timeout", 15.0) or 15.0)
    target = extract_bvid(ref)
    if not target:
        if _SHORT_RE.search(ref or ""):
            target = await resolve_short_link(ref, cfg=cfg)
        else:
            raise BiliError("没认出 BV 号或 av 号")
    params = {"bvid": target} if target.startswith("BV") else {"aid": target[2:]}
    data = await _get("/x/web-interface/view", params, timeout=timeout)

    stat = data.get("stat") or {}
    owner = data.get("owner") or {}
    bvid = str(data.get("bvid") or target)
    video = BiliVideo(
        bvid=bvid,
        aid=_as_int(data.get("aid")),
        title=str(data.get("title") or ""),
        author=str(owner.get("name") or ""),
        desc=str(data.get("desc") or ""),
        duration=_fmt_duration(data.get("duration")),
        play=_as_int(stat.get("view")),
        danmaku=_as_int(stat.get("danmaku")),
        like=_as_int(stat.get("like")),
        pubdate=_as_int(data.get("pubdate")),
        url=f"https://www.bilibili.com/video/{bvid}",
    )

    if comment_limit > 0 and video.aid:
        try:
            reply = await _get(
                "/x/v2/reply",
                {
                    "type": 1,
                    "oid": video.aid,
                    "sort": 2,  # 2 = 按热度
                    "ps": max(1, min(int(comment_limit), 20)),
                    "pn": 1,
                },
                timeout=timeout,
            )
            for row in (reply.get("replies") or [])[:comment_limit]:
                if not isinstance(row, dict):
                    continue
                member = row.get("member") or {}
                content = row.get("content") or {}
                text = str(content.get("message") or "").replace("\n", " ").strip()
                if not text:
                    continue
                video.comments.append(
                    (
                        str(member.get("uname") or ""),
                        text,
                        _as_int(row.get("like")),
                    )
                )
        except BiliError:
            # 评论区挂了不影响主体信息，静默跳过（关评论区/风控都会这样）
            pass
    return video


def render_hot(items: list[BiliVideo], *, limit: int = 10) -> str:
    if not items:
        return "【系统看到的 B站热门】\n（这次没拿到，榜单空了或者接口抽风）"
    lines = ["【系统看到的 B站热门】"]
    for idx, item in enumerate(items[:limit], start=1):
        meta = "｜".join(
            part
            for part in (
                f"UP：{item.author}" if item.author else "",
                f"播放 {_fmt_count(item.play)}" if item.play else "",
                item.duration,
            )
            if part
        )
        lines.append(f"{idx}. {item.title}" + (f"（{meta}）" if meta else ""))
        if item.url:
            lines.append(f"   {item.url}")
    lines.append(
        "这是你自己在 B 站首页看到的，别提「你发的」。"
        "标题只是标题——你没点开视频，不要编里面讲了什么。"
    )
    return "\n".join(lines)


def render_search(items: list[BiliVideo], *, keyword: str = "", limit: int = 8) -> str:
    head = f"【系统在 B站搜到的「{keyword}」】" if keyword else "【系统在 B站搜到的】"
    if not items:
        return head + "\n（一条都没有，换个说法再搜）"
    lines = [head]
    for idx, item in enumerate(items[:limit], start=1):
        meta = "｜".join(
            part
            for part in (
                f"UP：{item.author}" if item.author else "",
                f"播放 {_fmt_count(item.play)}" if item.play else "",
                item.duration,
                _fmt_date(item.pubdate),
            )
            if part
        )
        lines.append(f"{idx}. {item.title}" + (f"（{meta}）" if meta else ""))
        if item.desc:
            lines.append(f"   简介：{item.desc[:80]}")
        if item.url:
            lines.append(f"   {item.url}")
    lines.append(
        "这些是搜索结果，不是你看过的视频。想聊内容就用 <<<BILI video BV号>>> 去看某一条。"
    )
    return "\n".join(lines)


def render_video(video: BiliVideo, *, focus: str = "") -> str:
    lines = ["【系统看到的 B站视频】"]
    if focus:
        lines.append(f"（重点看：{focus}）")
    lines.append(f"标题：{video.title}")
    meta = "｜".join(
        part
        for part in (
            f"UP：{video.author}" if video.author else "",
            f"播放 {_fmt_count(video.play)}" if video.play else "",
            f"弹幕 {_fmt_count(video.danmaku)}" if video.danmaku else "",
            f"点赞 {_fmt_count(video.like)}" if video.like else "",
            video.duration,
            _fmt_date(video.pubdate),
        )
        if part
    )
    if meta:
        lines.append(meta)
    if video.desc:
        lines.append(f"简介：{video.desc[:300]}")
    if video.url:
        lines.append(f"链接：{video.url}")
    if video.comments:
        lines.append("热评：")
        for name, text, like in video.comments:
            suffix = f"（赞 {_fmt_count(like)}）" if like else ""
            lines.append(f"- {name}：{text[:100]}{suffix}")
    else:
        lines.append("热评：这次没拿到（可能关了评论或者被风控）")
    lines.append(
        "以上都是文字信息。你没看画面、没听声音，"
        "不要描述视频里出现了什么、说了什么。"
    )
    return "\n".join(lines)


# ------------------------------------------------------------------ 登录态与留言


_LOGIN_HINT = (
    "B站还没登录过。先在仓库根目录跑一次：python scripts\\login-web.py bili"
    "（要**小号**，脚本会拒收主号）"
)


def _state_cookies(cfg: Any = None) -> dict[str, str]:
    """从登录态文件里取 cookie。没有就抛，别静默降级成匿名。"""
    try:
        from .web_login import state_path
    except Exception as exc:  # noqa: BLE001
        raise BiliError(f"读不到登录模块：{exc}") from exc
    path = state_path("bili", cfg)
    if not path.exists():
        raise BiliError(_LOGIN_HINT)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BiliError(f"登录态文件读不出来：{exc}") from exc
    cookies: dict[str, str] = {}
    for item in data.get("cookies") or []:
        if isinstance(item, dict) and item.get("name"):
            cookies[str(item["name"])] = str(item.get("value") or "")
    if not cookies.get("SESSDATA"):
        raise BiliError(_LOGIN_HINT)
    return cookies


async def bili_whoami(*, cfg: Any = None) -> dict[str, Any]:
    """我是谁（登录态里那个号）。用来确认没登成主号、也用来在回复里分清谁是谁。"""
    cookies = _state_cookies(cfg)
    try:
        async with httpx.AsyncClient(
            headers=_HEADERS, cookies=cookies, timeout=20.0, follow_redirects=True
        ) as client:
            resp = await client.get(f"{_API}/x/web-interface/nav")
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise BiliError(f"问不到 B 站我是谁（{type(exc).__name__}）") from exc
    data = payload.get("data") or {}
    if not data.get("isLogin"):
        raise BiliError("B站登录态失效了，重跑一次 python scripts\\login-web.py bili")
    return {
        "mid": str(data.get("mid") or ""),
        "uname": str((data.get("uname") or "")).strip(),
        "level": _as_int((data.get("level_info") or {}).get("current_level")),
    }


@dataclass
class BiliMessage:
    """「回复我的」里的一条。"""

    kind: str = ""            # reply / at / like
    user: str = ""
    text: str = ""            # 对方说的话
    oid: int = 0              # 视频 aid
    bvid: str = ""
    root_id: int = 0          # 楼层根评论 id
    parent_id: int = 0        # 对方那条评论 id（回复时当 parent）
    my_text: str = ""         # 我原来那条（有就带上，方便她判断上下文）
    created: int = 0
    message_id: str = ""      # 用 (oid, parent_id) 组成的稳定去重键

    @property
    def replyable(self) -> bool:
        return bool(self.oid and self.parent_id)


def _parse_msgfeed(payload: dict[str, Any]) -> list[BiliMessage]:
    """解析 x/msgfeed/reply 的返回。单独拎出来是为了能单测。"""
    items = ((payload.get("data") or {}).get("items")) or []
    out: list[BiliMessage] = []
    for row in items:
        if not isinstance(row, dict):
            continue
        item = row.get("item") if isinstance(row.get("item"), dict) else {}
        reply = row.get("reply") if isinstance(row.get("reply"), dict) else {}
        kind = str(item.get("type") or row.get("type") or "").strip().lower()
        if kind and kind != "reply":
            # at / like 不算"有人回我"：@ 多是群发，点赞不需要回复
            continue
        # B 站这套字段的历史包袱很重：oid 在 reply 里最可靠，root 有时在 item 上，
        # 有时压根没有（对方是顶层评论时 root 就是它自己）。逐个兜底。
        oid = _as_int(reply.get("oid") or item.get("subject_id") or 0)
        parent_id = _as_int(reply.get("id") or item.get("source_id") or 0)
        root_id = _as_int(
            reply.get("root") or item.get("root_id") or item.get("target_id") or 0
        )
        if not root_id:
            root_id = parent_id
        content = reply.get("content") if isinstance(reply.get("content"), dict) else {}
        user = row.get("user") if isinstance(row.get("user"), dict) else {}
        target = reply.get("target_reply") if isinstance(reply.get("target_reply"), dict) else {}
        target_content = (
            target.get("content") if isinstance(target.get("content"), dict) else {}
        )
        out.append(
            BiliMessage(
                kind=kind or "reply",
                user=str(user.get("nickname") or "").strip(),
                text=_strip_em(str(content.get("message") or "")).strip(),
                oid=oid,
                root_id=root_id,
                parent_id=parent_id,
                my_text=_strip_em(str(target_content.get("message") or "")).strip(),
                created=_as_int(reply.get("ctime") or row.get("ctime") or 0),
                message_id=f"{oid}:{parent_id}",
            )
        )
    return out


async def bili_messages(*, cfg: Any = None, limit: int = 20) -> list[BiliMessage]:
    """拉「回复我的」。这是 B 站官方的消息接口，比爬页面稳得多。

    只看 type=reply（有人回我）；at/like 先不管：@ 可能是群发广告，
    点赞不需要回复，把它们混进来只会让她的留言箱变噪音。
    """
    cookies = _state_cookies(cfg)
    try:
        async with httpx.AsyncClient(
            headers=_HEADERS, cookies=cookies, timeout=25.0, follow_redirects=True
        ) as client:
            resp = await client.get(
                f"{_API}/x/msgfeed/reply", params={"pn": 1, "ps": max(1, min(limit, 30))}
            )
            payload = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise BiliError(f"拉不到 B 站留言（{type(exc).__name__}）") from exc
    code = _as_int(payload.get("code"))
    if code in (-101, -400):
        raise BiliError("B站登录态失效了，重跑一次 python scripts\\login-web.py bili")
    if code != 0:
        raise BiliError(f"拉不到 B 站留言：code={code} {payload.get('message') or ''}".strip())

    return _parse_msgfeed(payload)


async def bili_oid_of(ref: str, *, cfg: Any = None) -> tuple[int, str]:
    """把 BV 号/链接换成 aid（发评论要 oid=aid）。顺带把 bvid 带回来。"""
    bvid = extract_bvid(ref)
    if not bvid:
        raise BiliError("没认出 BV 号或 av 号")
    if bvid.startswith("av"):
        return int(bvid[2:]), ""
    data = await _get("/x/web-interface/view", {"bvid": bvid}, timeout=20.0)
    return _as_int(data.get("aid") or 0), str(data.get("bvid") or bvid)


async def bili_comment(
    ref: str,
    text: str,
    *,
    cfg: Any = None,
    root_id: int = 0,
    parent_id: int = 0,
) -> dict[str, Any]:
    """在视频下发评论 / 回某条评论。

    为什么这条不用浏览器：B 站评论区有**公开写接口**，只要带上
    SESSDATA（登录票据）+ bili_jct（csrf）就能发，不像小黑盒要页面签名。
    代价是它风控更看账号（新号、低频、短时间连发都容易 -412），
    所以调用方必须配合节奏护栏。

    root_id/parent_id 都为 0 = 发顶层评论；否则就是楼中楼回复：
    root 是该楼层根评论，parent 是被回复的那条。
    """
    cookies = _state_cookies(cfg)
    csrf = cookies.get("bili_jct") or ""
    if not csrf:
        raise BiliError("登录态里没有 bili_jct（csrf），重跑一次 login-web.py bili")
    oid, bvid = await bili_oid_of(ref, cfg=cfg)
    if not oid:
        raise BiliError("没拿到视频 aid，发不出去")

    payload = {"oid": oid, "type": 1, "message": text, "csrf": csrf, "plat": 1}
    if root_id:
        payload["root"] = int(root_id)
    if parent_id:
        payload["parent"] = int(parent_id)

    try:
        async with httpx.AsyncClient(
            headers={**_HEADERS, "Referer": f"https://www.bilibili.com/video/{bvid or 'av' + str(oid)}"},
            cookies=cookies,
            timeout=25.0,
            follow_redirects=True,
        ) as client:
            resp = await client.post(f"{_API}/x/v2/reply/add", data=payload)
            body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise BiliError(f"发评论的请求没打通（{type(exc).__name__}）") from exc

    code = _as_int(body.get("code"))
    if code == 0:
        data = body.get("data") if isinstance(body.get("data"), dict) else {}
        return {
            "ok": True,
            "rpid": _as_int(data.get("rpid") or 0),
            "oid": oid,
            "bvid": bvid,
            "text": text,
        }
    # 把这些码翻成人话，别让她只看到数字
    hints = {
        -101: "登录态失效了，重跑 login-web.py bili",
        -111: "csrf 校验失败（bili_jct 过期），重跑 login-web.py bili",
        -400: "参数不对（视频 id 或内容不合法）",
        -404: "评论区关了或者找不到这个视频",
        -412: "被 B 站风控挡了，先停一会儿",
        -509: "发得太频繁了，过一会儿再说",
        -702: "账号被限制发言（可能等级/实名不够）",
        # 实测（2026-10-26）：Lv0 新号发评论直接被拒，就是这一条
        -107: "账号还没转正（Lv0）。去 B站 账号中心 → 转正答题 考过就能发评论了",
        # 实测（2026-10-26）：转正到 Lv1 之后依然不让发，B 站的口子是 Lv2
        12074: "B站要求 Lv2 才能评论。这号现在是 Lv1，得再攒经验升一级",
        12002: "评论区已关闭",
        12009: "评论内容被审核拦截",
        12051: "已经发过一样的评论了",
    }
    hint = hints.get(code) or str(body.get("message") or "").strip() or "未知错误"
    raise BiliError(f"B站没接受这条评论：code={code} {hint}")


def render_messages(rows: list[BiliMessage], *, limit: int = 6) -> str:
    """把「回复我的」排成能读的样子，每条都带上回复要用的 id。"""
    if not rows:
        return "B站暂时没有回你的消息。"
    lines = [f"B站有人回你（{len(rows)} 条）："]
    for row in rows[:limit]:
        lines.append(f"- {row.user or '某人'}：{row.text[:60]}")
        if row.my_text:
            lines.append(f"   （你原来说的：{row.my_text[:40]}）")
        if row.replyable:
            lines.append(
                f"   回这条：<<<BILIPOST reply {row.bvid or 'av' + str(row.oid)}|"
                f"{row.root_id}|{row.parent_id}|<你要说的话>>>"
            )
    if len(rows) > limit:
        lines.append(f"（还有 {len(rows) - limit} 条没列）")
    return "\n".join(lines)
