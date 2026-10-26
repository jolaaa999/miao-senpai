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
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

__all__ = [
    "BiliError",
    "BiliMessage",
    "BiliVideo",
    "bili_comment",
    "bili_hot",
    "bili_messages",
    "bili_oid_of",
    "bili_whoami",
    "bili_search",
    "bili_video",
    "extract_bvid",
    "render_hot",
    "render_messages",
    "render_search",
    "render_video",
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


async def _get(path: str, params: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    """调一个接口并检查 code。风控（-352）时换一份新 cookie 重试一次。"""
    for attempt in (0, 1):
        cookies = await _ensure_cookies()
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


async def bili_hot(limit: int = 10, *, cfg: Any = None) -> list[BiliVideo]:
    """综合热门。用 popular 而不是 ranking/v2——后者匿名必被 -352。"""
    timeout = float(getattr(cfg, "bili_timeout", 15.0) or 15.0)
    data = await _get(
        "/x/web-interface/popular",
        {"ps": max(1, min(int(limit), 30)), "pn": 1},
        timeout=timeout,
    )
    rows = data.get("list") or []
    return [_video_from_popular(r) for r in rows if isinstance(r, dict)][:limit]


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
