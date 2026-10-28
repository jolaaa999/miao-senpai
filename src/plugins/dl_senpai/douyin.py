"""抖音只读层：推荐流、单个视频、热评。全部走浏览器。

为什么不用 httpx 直接打接口
--------------------------
抖音 web 的每个 /aweme/v1/web/* 请求都带 a_bogus（前身 X-Bogus）签名，
签名是一段混淆 JS，跟着前端版本变。自己复刻等于每隔几周修一次，
而且被封的成本是账号不是脚本。

所以这里换成：真浏览器打开页面，**拦截抖音自己发出的请求的响应**。
签名由它的页面完成，我们只读回来的 JSON——跟小黑盒那套一个思路。

代价与限制（说清楚，别指望它稳）
- 慢：一次读要开一次 chromium，10~20 秒
- 必须登录：匿名打开首页会弹登录框，推荐流不加载
- 解析对不上时不要硬猜：paylaod 结构变了就把原始响应 dump 出来再改
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "DouyinError",
    "DouyinVideo",
    "dy_feed",
    "dy_video",
    "parse_comments",
    "parse_feed",
    "render_feed",
    "render_video",
]

_HOME = "https://www.douyin.com/jingxuan"
_VIDEO_URL = "https://www.douyin.com/video/{aweme_id}"
# 推荐流接口。实测（2026-10-27）PC 端「精选」页走的是 v2 module/feed，
# 一次只回 3 条，往下滚才继续；tab/feed 是「推荐」页签的老口子，留着兜底。
_FEED_HINTS = (
    "/aweme/v2/web/module/feed",
    "/aweme/v1/web/module/feed",
    "/aweme/v1/web/tab/feed",
)
_COMMENT_HINT = "/aweme/v1/web/comment/list"
_DETAIL_HINT = "/aweme/v1/web/aweme/detail"

_LOGIN_HINT = "抖音登录态没了，重跑一次 python scripts\\login-web.py douyin"
_DEBUG_DIR = Path("data/dl_senpai/douyin_raw")


class DouyinError(Exception):
    """抖音读失败。message 是能直接给主人看的一句话。"""


@dataclass
class DouyinVideo:
    """一个视频的最小信息集——够她聊天用，不多抓。"""

    aweme_id: str = ""
    desc: str = ""
    author: str = ""
    digg: int = 0
    comment_count: int = 0
    share: int = 0
    duration_sec: int = 0
    create_time: int = 0
    url: str = ""
    music: str = ""
    # 视频直链：抽帧看画面要用（feed 里 play_addr.url_list 第一个）
    play_url: str = ""
    comments: list[tuple[str, str, int]] = field(default_factory=list)


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fmt_count(num: int) -> str:
    if num >= 100000000:
        return f"{num / 100000000:.1f}亿"
    if num >= 10000:
        return f"{num / 10000:.1f}万"
    return str(num)


def _first_url(node: Any) -> str:
    """封面/头像这类结构统一是 {url_list: [...]}，取第一张。"""
    if isinstance(node, dict):
        urls = node.get("url_list") or []
        if isinstance(urls, list) and urls:
            return str(urls[0])
    return ""


def _nickname(node: Any) -> str:
    if isinstance(node, dict):
        return str(node.get("nickname") or node.get("unique_id") or "")
    return ""


def parse_feed(payload: dict[str, Any]) -> list[DouyinVideo]:
    """从推荐流响应里挑出视频。结构对不上就返回空表，不猜。

    真实的响应是 {"aweme_list": [...], "has_more": 1}；每个 item 里
    desc/author/statistics/video 都可能缺失（广告、直播卡片就没有），
    所以字段一律 .get，宁缺毋滥。
    """
    rows = payload.get("aweme_list") if isinstance(payload, dict) else None
    out: list[DouyinVideo] = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        aweme_id = str(item.get("aweme_id") or "")
        if not aweme_id:
            continue
        # 直播卡片没有 video.duration，混进来会让她"刷"到一个不存在的视频
        video = item.get("video")
        # 直播卡片整个 video 字段都没有（拿到的是空 dict），别当成视频
        if not isinstance(video, dict) or not video:
            continue
        stat = item.get("statistics") or {}
        # 播放直链：抽帧看画面要用它。play_addr / h264 / 老字段三处兜底
        play = video.get("play_addr") or video.get("play_addr_h264") or {}
        urls = play.get("url_list") or []
        out.append(
            DouyinVideo(
                aweme_id=aweme_id,
                desc=" ".join(str(item.get("desc") or "").split())[:200],
                author=_nickname(item.get("author")),
                digg=_as_int(stat.get("digg_count")),
                comment_count=_as_int(stat.get("comment_count")),
                share=_as_int(stat.get("share_count")),
                duration_sec=_as_int(video.get("duration")) // 1000,
                create_time=_as_int(item.get("create_time")),
                url=_VIDEO_URL.format(aweme_id=aweme_id),
                music=_nickname((item.get("music") or {}) if isinstance(item.get("music"), dict) else {}),
                play_url=str(urls[0]) if urls else "",
            )
        )
    return out


def parse_comments(payload: dict[str, Any]) -> list[tuple[str, str, int]]:
    """热评：(昵称, 内容, 点赞数)。结构不对就返回空表。"""
    rows = payload.get("comments") if isinstance(payload, dict) else None
    out: list[tuple[str, str, int]] = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("text") or "").split())
        if not text:
            continue
        out.append((_nickname(item.get("user")), text[:120], _as_int(item.get("digg_count"))))
    return out


def _dump(cfg: Any, name: str, payload: Any) -> None:
    """把原始响应存一份：解析对不上时靠它改代码，不用再去猜接口长什么样。"""
    if not bool(getattr(cfg, "douyin_dump_raw", False)):
        return
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        path = _DEBUG_DIR / f"{name}-{int(time.time())}.json"
        path.write_text(json.dumps(payload, ensure_ascii=False)[:400000], encoding="utf-8")
    except OSError:
        pass


def _state_or_raise(cfg: Any) -> str:
    from .web_login import optional_state_path

    state = optional_state_path("douyin", cfg)
    if not state:
        raise DouyinError(_LOGIN_HINT)
    return state


async def _with_douyin_page(cfg: Any, fn: Any) -> Any:
    from .browser_agent import BrowseError, _with_page
    from .web_login import state_path

    state = _state_or_raise(cfg)
    try:
        return await _with_page(
            cfg, fn, storage_state=state, persist_path=str(state_path("douyin", cfg))
        )
    except BrowseError as exc:
        raise DouyinError(str(exc)) from exc


async def dy_feed(*, cfg: Any = None, limit: int = 12) -> list[DouyinVideo]:
    """读推荐流。取不到就返回空表（"这次没刷到"不是错误）。"""
    timeout = float(getattr(cfg, "douyin_timeout", 40.0) or 40.0)
    collected: list[DouyinVideo] = []

    async def _run(page: Any) -> list[DouyinVideo]:
        captured: list[dict[str, Any]] = []

        def _hook(response: Any) -> None:
            if any(hint in response.url for hint in _FEED_HINTS):
                captured.append(response)

        page.on("response", _hook)
        try:
            await page.goto(_HOME, wait_until="domcontentloaded", timeout=int(timeout * 1000))
        except Exception as exc:  # noqa: BLE001 — playwright 异常类型很杂
            raise DouyinError(f"抖音首页打不开（{type(exc).__name__}）") from exc

        # 推荐流是滚动触发的：先等，等不到再滚两下逼它加载
        deadline = time.time() + timeout
        rounds = 0
        # 实测一遍只回 3 条，想要 10 条以上就得往下滚；滚到抓够了或超时为止
        while time.time() < deadline and rounds < 6 and len(captured) < 5:
            await asyncio.sleep(3.5)
            try:
                await page.mouse.wheel(0, 2200)
            except Exception:  # noqa: BLE001
                break
            rounds += 1

        videos: list[DouyinVideo] = []
        for response in captured[:6]:
            try:
                payload = await response.json()
            except Exception:  # noqa: BLE001 — 有的响应不是 JSON（预加载/占位）
                continue
            _dump(cfg, "feed", payload)
            videos.extend(parse_feed(payload))
        return videos

    videos = await _with_douyin_page(cfg, _run)
    seen: set[str] = set()
    for video in videos:
        if video.aweme_id in seen:
            continue
        seen.add(video.aweme_id)
        collected.append(video)
        if len(collected) >= max(1, int(limit)):
            break
    return collected


async def dy_video(
    aweme_id: str, *, cfg: Any = None, comment_limit: int = 5
) -> DouyinVideo:
    """看一个视频：描述、作者、数据，外加几条热评。"""
    timeout = float(getattr(cfg, "douyin_timeout", 40.0) or 40.0)
    target = str(aweme_id or "").strip()
    if not target:
        raise DouyinError("没给视频 id")

    async def _run(page: Any) -> DouyinVideo:
        comments: list[tuple[str, str, int]] = []
        detail: dict[str, Any] = {}

        def _hook(response: Any) -> None:
            url = response.url
            if _COMMENT_HINT in url or _DETAIL_HINT in url:
                captured.append(response)

        captured: list[Any] = []
        page.on("response", _hook)
        try:
            await page.goto(
                _VIDEO_URL.format(aweme_id=target),
                wait_until="domcontentloaded",
                timeout=int(timeout * 1000),
            )
        except Exception as exc:  # noqa: BLE001
            raise DouyinError(f"这条视频打不开（{type(exc).__name__}）") from exc
        await asyncio.sleep(6.0)

        for response in captured:
            try:
                payload = await response.json()
            except Exception:  # noqa: BLE001
                continue
            if _COMMENT_HINT in response.url:
                _dump(cfg, "comments", payload)
                comments.extend(parse_comments(payload))
            elif _DETAIL_HINT in response.url:
                _dump(cfg, "detail", payload)
                rows = payload.get("aweme_detail") if isinstance(payload, dict) else None
                if isinstance(rows, dict):
                    detail = rows

        video = parse_feed({"aweme_list": [detail]})[0] if detail else DouyinVideo()
        if not video.aweme_id:
            # 详情接口没回（有时只有评论接口），退回读页面上的描述
            desc = ""
            try:
                desc = (await page.inner_text('[data-e2e="video-desc"]'))[:200]
            except Exception:  # noqa: BLE001 — 选择器改版就会抛，兜底而已
                desc = ""
            video = DouyinVideo(aweme_id=target, desc=desc, url=_VIDEO_URL.format(aweme_id=target))
        video.comments = comments[: max(0, int(comment_limit))]
        return video

    return await _with_douyin_page(cfg, _run)


def render_feed(videos: list[DouyinVideo], *, limit: int = 12) -> str:
    if not videos:
        return "抖音这次没刷出东西。"
    lines = [f"抖音推荐（{min(len(videos), limit)} 条）："]
    for i, video in enumerate(videos[:limit], 1):
        hot = _fmt_count(video.digg)
        lines.append(f"{i}. {video.desc[:46]}（{video.author or '?'} · 赞{hot}）")
    return "\n".join(lines)


def render_video(video: DouyinVideo, *, limit: int = 5) -> str:
    if not video.aweme_id:
        return "这条视频没读到内容。"
    lines = [
        f"《{video.desc or '(没描述)'}》",
        f"作者：{video.author or '?'}　赞 {_fmt_count(video.digg)}　评论 {_fmt_count(video.comment_count)}",
    ]
    if video.music:
        lines.append(f"背景音乐：{video.music}")
    if video.duration_sec:
        lines.append(f"时长：{video.duration_sec} 秒")
    if video.comments:
        lines.append("热评：")
        for user, text, digg in video.comments[:limit]:
            lines.append(f"  {user}：{text}（{_fmt_count(digg)}）")
    lines.append(video.url)
    return "\n".join(lines)
