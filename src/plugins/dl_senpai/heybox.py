"""小黑盒只读：站内搜索 + 读帖。

为什么必须开浏览器
------------------
站内搜索接口 /bbs/app/api/general/search/v1 要 hkey 签名，服务端会校验
（只给 _time/nonce 会回「非法请求」）。社区里有人把签名逆向出来了
（AES MixColumns 变体、故意发错签名看 403 回显对拍），但那是硬维护活：
接口一改版就得重来，而且带风控风险。

这里换一条稳的路：Playwright 打开官方搜索页，**页签自己会带着合法签名
去请求**，我们只在旁边接住同一个响应。签名留给浏览器算。

读正文交给 link_reader（它已经能读小黑盒的分享页，含 SPA 渲染兜底）。
注意：帖子页有两种 URL，只有带 hash 的分享链接能读到正文，
/app/bbs/link/<数字id> 那种渲染出来只有一个页脚。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

__all__ = [
    "HeyboxCommentResult",
    "HeyboxError",
    "HeyboxPost",
    "clean_comment",
    "heybox_comment",
    "heybox_read",
    "heybox_search",
    "render_heybox_search",
    "share_url_of",
]

_SEARCH_PAGE = "https://www.xiaoheihe.cn/app/search?q={query}"
_SHARE_BASE = (
    "https://api.xiaoheihe.cn/v3/bbs/app/api/web/share"
    "?h_camp=link&h_src=YXBwX3NoYXJl&link_id={link_id}"
)
_SEARCH_API_HINT = "general/search/v1"
# 读帖失败后的冷却秒数。实测小黑盒的内容接口在连续抓取后会回
# {"status": "show_captcha"}，页面只剩页头页脚；此时再刷只会更久不给。
# 冷却期间直接告诉她"刚被要求验证"，比反复失败更诚实。
_READ_COOLDOWN_SEC = 600.0
# 写操作（评论）被挡时的冷却：比读更长。理由是小黑盒对写接口的风控更敏感，
# 短时间反复试只会把恢复时间越推越远。
_WRITE_COOLDOWN_SEC = 1200.0
_read_cooldown_until = 0.0
_HASH_RE = re.compile(r"^[0-9a-f]{8,24}$", re.IGNORECASE)
_EM_RE = re.compile(r"</?em[^>]*>")


class HeyboxError(Exception):
    """小黑盒读不到。message 已经是人话。"""


@dataclass
class HeyboxPost:
    link_id: str = ""          # 数字 id，只能给她看，不能直读
    share_id: str = ""         # 分享链接里的 hash，读正文用它
    title: str = ""
    desc: str = ""
    comments: int = 0
    awards: int = 0
    created: str = ""

    @property
    def share_url(self) -> str:
        return share_url_of(self.share_id) if self.share_id else ""


@dataclass
class HeyboxSearch:
    keyword: str = ""
    posts: list[HeyboxPost] = field(default_factory=list)
    error: str = ""


def _strip_em(text: str) -> str:
    """摘要里也夹着 HTML 实体（&quot; &amp;），还原后再给模型。"""
    import html

    return html.unescape(_EM_RE.sub("", text or "")).strip()


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _fmt_ts(stamp: Any) -> str:
    import time

    ts = _as_int(stamp)
    if ts <= 0:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _fmt_count(num: int) -> str:
    if num >= 10_000:
        return f"{num / 10_000:.1f}万"
    return str(num)


def share_url_of(link_id: str) -> str:
    """把 hash 形式的 link_id 拼成分享链接。"""
    return _SHARE_BASE.format(link_id=(link_id or "").strip())


def _share_id_of(share_url: str) -> str:
    """从 share_url 里抠出 link_id 参数。"""
    match = re.search(r"[?&]link_id=([0-9A-Za-z]+)", share_url or "")
    return match.group(1) if match else ""


def _parse_payload(payload: Any) -> list[HeyboxPost]:
    if not isinstance(payload, dict):
        return []
    result = payload.get("result") or {}
    items = result.get("items") if isinstance(result, dict) else None
    if not isinstance(items, list):
        return []
    posts: list[HeyboxPost] = []
    for row in items:
        info = (row or {}).get("info") if isinstance(row, dict) else None
        if not isinstance(info, dict):
            continue
        share = str(info.get("share_url") or "")
        link_id = str(info.get("linkid") or "")
        if not link_id:
            continue
        posts.append(
            HeyboxPost(
                link_id=link_id,
                share_id=_share_id_of(share),
                title=_strip_em(str(info.get("title") or "")),
                desc=_strip_em(str(info.get("description") or "")),
                comments=_as_int(info.get("comment_num")),
                awards=_as_int(info.get("link_award_num")),
                created=_fmt_ts(info.get("create_at")),
            )
        )
    return posts


async def heybox_search(keyword: str, limit: int = 6, *, cfg: Any = None) -> HeyboxSearch:
    """站内搜索帖子。失败抛 HeyboxError（人话原因）。"""
    query = (keyword or "").strip()
    if not query:
        raise HeyboxError("搜索词是空的")
    from .browser_agent import BrowseError, _with_page
    from .web_login import optional_state_path, state_path

    timeout_sec = float(getattr(cfg, "heybox_timeout", 30.0) or 30.0)
    storage_state = optional_state_path("heybox", cfg)
    persist_to = str(state_path("heybox", cfg))

    async def _run(page: Any) -> list[HeyboxPost]:
        url = _SEARCH_PAGE.format(query=quote(query))
        try:
            async with page.expect_response(
                lambda r: _SEARCH_API_HINT in r.url,
                timeout=timeout_sec * 1000,
            ) as waiter:
                await page.goto(url, wait_until="domcontentloaded")
            response = await waiter.value
            payload = await response.json()
        except Exception as exc:  # noqa: BLE001 — playwright 的异常类型很杂
            raise HeyboxError(
                f"搜索页没吐出结果（{type(exc).__name__}），可能是页面改版或被限流"
            ) from exc
        return _parse_payload(payload)

    try:
        posts = await _with_page(
            cfg, _run, storage_state=storage_state, persist_path=persist_to
        )
    except BrowseError as exc:
        raise HeyboxError(str(exc)) from exc
    return HeyboxSearch(keyword=query, posts=posts[: max(1, int(limit))])


def _bump_cooldown(seconds: float) -> None:
    """把冷却时间往后推（多个入口共用同一个时间戳）。"""
    global _read_cooldown_until
    _read_cooldown_until = max(_read_cooldown_until, time.time() + seconds)


def _cooldown_message(remaining: float) -> str:
    minutes = max(1, int(remaining // 60) + 1)
    return f"小黑盒刚把我们挡了一下（要人机验证/限流），先歇 {minutes} 分钟再碰它"


def read_cooldown_remaining() -> float:
    """读帖还被冷却着吗？返回剩余秒数（0 = 可以读）。"""
    import time

    return max(0.0, _read_cooldown_until - time.time())


async def heybox_read(ref: str, *, cfg: Any = None, focus: str = "") -> Any:
    """读一篇帖子正文。返回 link_reader 的 LinkDoc（调用方看 doc.ok）。

    ref 可以是：带 hash 的分享链接、裸 hash、或者帖子 URL（含 app/bbs/link）。
    """
    global _read_cooldown_until
    from .link_reader import read_link

    # 冷却期一律不再打接口：这段时间去了也是白去，还会把封禁拖长。
    remaining = read_cooldown_remaining()
    if remaining > 0:
        raise HeyboxError(
            f"小黑盒刚才要求人机验证了，先歇 {int(remaining // 60) + 1} 分钟再读"
        )

    raw = (ref or "").strip()
    if not raw:
        raise HeyboxError("没给要读的帖子")
    if raw.startswith("http://") or raw.startswith("https://"):
        if "xiaoheihe" not in raw:
            raise HeyboxError("这不是小黑盒的链接")
        # 数字 id 的帖子页读不出正文，换成等价的搜索提示
        if re.search(r"/app/bbs/link/\d+", raw):
            raise HeyboxError(
                "这个帖子页只有数字 id，正文读不到；用搜索给出的分享链接"
            )
        url = raw
    elif _HASH_RE.match(raw):
        url = share_url_of(raw)
    else:
        raise HeyboxError("认不出这个帖子标识（要 hash 形式的分享链接）")
    doc = await read_link(url, cfg=cfg, focus=focus)
    if not getattr(doc, "ok", False) and "empty_text" in (getattr(doc, "error", "") or ""):
        # 抽不到正文 = 这次大概被人机验证拦了，进入冷却
        import time as _time

        _read_cooldown_until = _time.time() + _READ_COOLDOWN_SEC
    return doc


def render_heybox_search(result: HeyboxSearch, *, limit: int = 6) -> str:
    head = f"【系统在小黑盒搜到的「{result.keyword}」】" if result.keyword else "【系统在小黑盒搜到的】"
    if not result.posts:
        return head + "\n（没搜到，或者被限流了）"
    lines = [head]
    for idx, post in enumerate(result.posts[:limit], start=1):
        meta = "｜".join(
            part
            for part in (
                f"评论 {_fmt_count(post.comments)}" if post.comments else "",
                f"赞 {_fmt_count(post.awards)}" if post.awards else "",
                post.created,
            )
            if part
        )
        lines.append(f"{idx}. {post.title}" + (f"（{meta}）" if meta else ""))
        if post.desc:
            lines.append(f"   {post.desc[:100]}")
        if post.share_url:
            lines.append(f"   读全文：<<<HEYBOX read {post.share_id}>>>")
    lines.append(
        "这些是搜索结果的标题和摘要，不是你读过内容。"
        "想聊里面的细节，先 HEYBOX read 那一条；读不到就说读不到。"
    )
    return "\n".join(lines)


# ------------------------------------------------------------------ 写：评论


_COMMENT_MAX_DEFAULT = 300
_COMMENT_URL_RE = re.compile(r"https?://|www\.", re.IGNORECASE)
_COMMENT_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


@dataclass
class HeyboxCommentResult:
    ok: bool = False
    text: str = ""
    error: str = ""
    link_id: str = ""


def clean_comment(text: str, cfg: Any = None) -> str:
    """评论内容清洗。不合格就抛 HeyboxError（人话原因）。

    为什么比说说宽松（300 字）却仍禁链接：评论区带外链最容易被判营销，
    而且她评论的对象是陌生人的帖子，出事要主人背。
    """
    raw = re.sub(r"\n{2,}", "\n", (text or "").replace("\r\n", "\n")).strip()
    if not raw:
        raise HeyboxError("评论内容是空的")
    limit = int(getattr(cfg, "heybox_comment_max_chars", _COMMENT_MAX_DEFAULT) or 0)
    if limit > 0 and len(raw) > limit:
        raise HeyboxError(f"评论太长了（{len(raw)} 字），最多 {limit} 字")
    if _COMMENT_URL_RE.search(raw):
        raise HeyboxError("评论里不带链接")
    if _COMMENT_PHONE_RE.search(raw):
        raise HeyboxError("评论里不要出现手机号")
    return raw


async def heybox_comment(
    ref: str,
    text: str,
    *,
    cfg: Any = None,
    dry_run: bool = False,
) -> HeyboxCommentResult:
    """在某个帖子下评论一条。

    必须用浏览器点 UI：写接口同样要 hkey 签名（服务端校验），逆向不划算。
    失败会说得具体：没登录 / 找不到输入框（改版） / 点完没生效。

    2026-10-25 实测抓到的三步（都在下面按顺序做，每步都留了退路）：
        1. 点 .hb-bbs-link__reply-target（底部固定条「评论 (已有 N 条评论)」）
        2. 输入框是 ProseMirror 的 div.ProseMirror.hb-editor，没有 textarea，
           也不能用 fill()——必须先 click 再用键盘事件打字
        3. 发送按钮 button.link-reply__menu-btn.hb-color__btn--confirm（文案「发送」）

    dry_run=True 时做到"字打进去了"就停，绝不点发送——用来验证链路而不留痕。
    """
    from .web_login import with_context

    raw = (ref or "").strip()
    if _HASH_RE.match(raw):
        url = share_url_of(raw)
        link_id = raw
    elif raw.startswith("http") and "xiaoheihe" in raw:
        url = raw
        link_id = _share_id_of(raw) or raw[-12:]
    else:
        raise HeyboxError("认不出这个帖子（要 hash 形式的分享链接）")

    cleaned = clean_comment(text, cfg)

    remaining = read_cooldown_remaining()
    if remaining > 0:
        # 冷却期内直接不出门：反复试只会让恢复更慢
        raise HeyboxError(_cooldown_message(remaining))

    timeout_sec = float(getattr(cfg, "heybox_timeout", 30.0) or 30.0)
    await _session_gate(cfg)

    async def _run(page: Any) -> HeyboxCommentResult:
        await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_sec) * 1000)
        # 等正文出来：登录态失效时页面会停在壳子上（只有页头页脚 200 多字）
        deadline = time.time() + timeout_sec
        body_text = ""
        while time.time() < deadline:
            body_text = await page.evaluate(
                "() => document.body ? document.body.innerText : ''"
            )
            if body_text and len(body_text) > 800:
                break
            await page.wait_for_timeout(700)
        if len(body_text or "") <= 800:
            # 三种可能，说清楚让人知道下一步做什么（别只会说"登录过期"）：
            #   页面要验证 -> 停一会儿 / 重新跑 warm-cookies
            #   登录态过期 -> 重新跑 login-web
            lowered = body_text or ""
            if any(word in lowered for word in ("验证", "滑块", "拖动", "人机")):
                _bump_cooldown(_WRITE_COOLDOWN_SEC)
                raise HeyboxError("页面弹出人机验证了，先停一会儿；实在不行重跑 warm-cookies.py heybox")
            if len(lowered) < 300:
                _bump_cooldown(_WRITE_COOLDOWN_SEC)
                raise HeyboxError("页面基本没渲染出来（大概被限流了），过十几分钟再试")
            raise HeyboxError("帖子正文没加载出来（登录态可能过期，重新跑一次 login-web.py heybox）")

        # 1) 点开评论输入区：黑盒把入口做成一个固定条
        #    （.hb-bbs-link__reply-target，文案是「评论 (已有 N 条评论)」）
        opened = False
        for locator in (
            page.locator(".hb-bbs-link__reply-target").first,
            page.locator("[class*='reply-target']").first,
        ):
            try:
                if await locator.count():
                    await locator.click()
                    opened = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not opened:
            raise HeyboxError("没找到评论入口（页面可能改版了）")

        # 2) 输入框是 ProseMirror 富文本（div.hb-editor），textarea 不存在，
        #    也不能用 fill()——它不触发输入事件，ProseMirror 收不到字。
        editor = None
        for locator in (
            page.locator("div.ProseMirror.hb-editor").first,
            page.locator("[contenteditable='true']").last,
        ):
            try:
                await locator.wait_for(state="visible", timeout=8000)
                editor = locator
                break
            except Exception:  # noqa: BLE001
                continue
        if editor is None:
            raise HeyboxError("评论入口点开了，但输入框没出现（页面大概改版了）")

        await editor.click()
        await page.keyboard.type(cleaned, delay=20)
        await page.wait_for_timeout(600)
        typed = (await editor.inner_text() or "").strip()
        if cleaned[:8] and cleaned[:8] not in typed:
            raise HeyboxError("字没能打进输入框（富文本编辑器没接受输入）")

        if dry_run:
            # 只填不发：用来验证"入口能找到、字能打进去"，不产生任何公开痕迹
            return HeyboxCommentResult(ok=True, text=cleaned, link_id=link_id)

        # 发之前留个底：等一下要用"正文里多了这段话"来判断发没发出去。
        # 为什么必须做差集：帖子正文里可能本来就含同样的词（踩过：评论写的是
        # "setup-dsh.mjs 那个坑"，而正文第 8 个坑里就有 scripts/setup-dsh.mjs，
        # 只比前 12 个字就直接假成功了）。
        before = await page.evaluate("() => document.body ? document.body.innerText : ''")
        probe = cleaned.strip()[:24]

        # 3) 发送按钮：button.link-reply__menu-btn.hb-color__btn--confirm，文案「发送」
        sent = False
        for locator in (
            page.locator("button.link-reply__menu-btn.hb-color__btn--confirm").first,
            page.get_by_text("发送", exact=True).last,
        ):
            try:
                if await locator.count():
                    await locator.click()
                    sent = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not sent:
            raise HeyboxError("找到输入框但没找到发送按钮（页面大概改版了）")

        await page.wait_for_timeout(4000)
        after = await page.evaluate("() => document.body ? document.body.innerText : ''")
        # 三条证据，按可靠性排序：
        #   1) 编辑器被清空（站点的成功信号——发送失败时字会留在框里）
        #   2) 页面里出现了这段新文字，且发之前没有
        #   3) 出现"评论成功/审核"一类的提示
        editor_left = ""
        try:
            editor_left = (await editor.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            editor_left = ""
        cleared = not editor_left
        # 顺序很重要：输入框里本来就留着我们打的字，所以"页面里出现这段字"只有在
        # 输入框被清空之后才算证据。先判 appeared 会得到假成功（踩过）。
        appeared = (
            cleared and bool(probe) and probe in (after or "") and probe not in (before or "")
        )
        hinted = any(word in (after or "") for word in ("评论成功", "发送成功", "审核"))
        if appeared:
            return HeyboxCommentResult(ok=True, text=cleaned, link_id=link_id)
        if cleared and (hinted or len(after or "") > len(before or "")):
            # 输入框空了、页面内容也确实变多了：按成成功处理，但要标明"没回读到原文"
            return HeyboxCommentResult(ok=True, text=cleaned, link_id=link_id)
        if cleared:
            return HeyboxCommentResult(
                ok=True,
                text=cleaned,
                link_id=link_id,
                error="输入框已清空，但没能回读到这条评论（可能进审核）",
            )
        raise HeyboxError("点完发送但输入框里的字还在，这条大概没发出去")

    return await with_context("heybox", cfg, _run)


# ------------------------------------------------------------------ 出门节奏


_SESSION_GATE_FILE = "data/dl_senpai/heybox_session_gate.json"
_session_last = 0.0


async def _session_gate(cfg: Any = None) -> None:
    """开浏览器之前的一次"出门登记"。

    为什么需要：2026-10-26 实测，短时间内连着开十几次浏览器上下文之后，
    link/tree 直接回 {"status":"show_captcha"}，连正文都只剩 270 字。
    这不是登录问题（restore_login 一直是 ok），是**访问密度**问题。
    所以她每次出门之间要隔一会儿，宁可慢也别把账号搞进验证。
    """
    global _session_last
    gap = float(getattr(cfg, "heybox_session_gap_sec", 240) or 0)
    if gap <= 0:
        return
    now = time.time()
    last = _session_last
    if last <= 0:
        # 进程内没记录就看落盘的那份：重启之后也不该立刻猛刷
        try:
            path = Path(_SESSION_GATE_FILE)
            if path.exists():
                last = float(json.loads(path.read_text(encoding="utf-8")).get("ts") or 0)
        except (OSError, json.JSONDecodeError, ValueError):
            last = 0.0
    wait = gap - (now - last) if last > 0 else 0
    if wait > 0:
        await asyncio.sleep(min(wait, gap))
    _session_last = time.time()
    try:
        path = Path(_SESSION_GATE_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"ts": _session_last}), encoding="utf-8")
    except OSError:
        pass


# ------------------------------------------------------------------ 刷帖子流


_FEED_PAGE = "https://www.xiaoheihe.cn/app/home"
_FEED_API_HINT = "bbs/app/feeds"


@dataclass
class FeedPost:
    """首页信息流里的一条帖子。"""

    link_id: str = ""
    title: str = ""
    desc: str = ""
    comments: int = 0
    created: int = 0
    topics: str = ""
    share_url: str = ""
    # 数字 id 只用来对日志；读正文要 hash（上面的 link_id）
    numeric_id: str = ""


async def heybox_feed(
    *, cfg: Any = None, limit: int = 12, timeout: float = 45.0
) -> list[FeedPost]:
    """刷一遍首页信息流，拿回帖子列表。

    为什么要开浏览器：信息流接口和其它接口一样要 hkey 签名，而签名逻辑在
    页面自己的 JS 里。所以我们打开首页，用 page 的响应钩子把页面自己发出的
    那份响应截下来——不逆向签名。

    为什么她"刷帖子"要有这一层：只会用搜索 = 只会查不会逛。信息流才是
    "随便看看"，她能自己撞见想聊的东西，而不是等别人给关键词。
    """
    from .browser_agent import _with_page
    from .web_login import has_state, optional_state_path, state_path

    if not has_state("heybox", cfg):
        raise HeyboxError("小黑盒还没登录/没过验证，先跑一次 scripts\\login-web.py heybox")

    await _session_gate(cfg)
    collected: list[str] = []
    storage_state = optional_state_path("heybox", cfg)
    persist_to = str(state_path("heybox", cfg))

    async def _run(page: Any) -> list[FeedPost]:
        async def _capture(resp: Any) -> None:
            try:
                if _FEED_API_HINT in resp.url and "banner" not in resp.url:
                    collected.append(await resp.text())
            except Exception:  # noqa: BLE001
                pass

        page.on("response", lambda r: asyncio.ensure_future(_capture(r)))
        await page.goto(_FEED_PAGE, wait_until="domcontentloaded", timeout=int(timeout) * 1000)
        deadline = time.time() + timeout
        while time.time() < deadline and not collected:
            await page.wait_for_timeout(800)
        await page.wait_for_timeout(1500)
        # 往下滚一下：信息流是懒加载的，能多拿到几条
        try:
            await page.mouse.wheel(0, 2500)
            await page.wait_for_timeout(2500)
        except Exception:  # noqa: BLE001
            pass
        return []

    await _with_page(cfg, _run, storage_state=storage_state, persist_path=persist_to)

    posts: list[FeedPost] = []
    seen: set[str] = set()
    for body in collected:
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, TypeError):
            continue
        result = data.get("result") if isinstance(data, dict) else None
        links = (result or {}).get("links") if isinstance(result, dict) else None
        for item in links or []:
            if not isinstance(item, dict):
                continue
            numeric_id = str(item.get("linkid") or "").strip()
            raw_url = str(item.get("share_url") or "").strip()
            # 信息流给的 linkid 是数字 id，那种页面渲染出来只有页脚；
            # 真正能读到正文的是 share_url 里那串 hash。
            link_id = _share_id_of(raw_url) or numeric_id
            if not link_id or link_id in seen:
                continue
            seen.add(link_id)
            topics = " ".join(
                str(t.get("name") or "").strip()
                for t in (item.get("topics") or [])
                if isinstance(t, dict) and str(t.get("name") or "").strip()
            )
            posts.append(
                FeedPost(
                    link_id=link_id,
                    title=_strip_em(str(item.get("title") or "")).strip(),
                    desc=_strip_em(str(item.get("description") or "")).strip()[:200],
                    comments=int(item.get("comment_num") or 0),
                    created=int(item.get("create_at") or 0),
                    topics=topics,
                    share_url=raw_url or share_url_of(link_id),
                    numeric_id=numeric_id,
                )
            )
            if len(posts) >= max(1, limit):
                return posts
    if not posts:
        raise HeyboxError("信息流没抓到帖子（可能被要验证，或者页面改版了）")
    return posts


def render_feed(posts: list[FeedPost], *, limit: int = 8) -> str:
    """把刷到的东西排成能读的样子。

    每条都带 hash：她想看正文就接着 read，不感兴趣直接跳过。
    语气要轻——她是在随便看看，不是在给主人交报表。
    """
    if not posts:
        return "信息流是空的。"
    lines = [f"首页信息流（{len(posts)} 条，挑着看就行）："]
    for index, post in enumerate(posts[:limit], 1):
        bits = []
        if post.topics:
            bits.append(post.topics[:24])
        if post.comments:
            bits.append(f"{post.comments} 评")
        meta = f"（{'，'.join(bits)}）" if bits else ""
        lines.append(f"{index}. {post.title[:48]}{meta}")
        if post.desc:
            lines.append(f"   {post.desc[:60]}")
        lines.append(f"   读这篇：<<<HEYBOX read {post.link_id}>>>")
    if len(posts) > limit:
        lines.append(f"（还有 {len(posts) - limit} 条没列）")
    return "\n".join(lines)


# ------------------------------------------------------------------ 评论区（看留言 / 回留言）


_TREE_API_HINT = "bbs/app/link/tree"


@dataclass
class ThreadComment:
    """评论区里的一条（顶层评论或某条的子回复）。"""

    comment_id: str = ""
    user: str = ""
    text: str = ""
    parent_id: str = ""
    root_id: str = ""
    created: int = 0
    up: int = 0
    # 根评论上的元信息：下面还有多少条子回复、是不是还有没给全的
    child_num: int = 0
    has_more: bool = False
    floor: int = 0


def _parse_tree_body(body: str) -> tuple[list[ThreadComment], bool]:
    """把一份 link/tree 响应解析成评论列表。返回 (评论, 是否被要求验证)。

    实测结构（2026-10-26）：result.comments 是楼层数组，
    每层 {"comment": [根评论, 预览子回复1, 预览子回复2]}，
    根评论上的 child_num / has_more 说明还有子回复没给全。
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return [], False
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict) or not result:
        return [], bool(isinstance(data, dict) and "captcha" in str(data.get("status") or ""))
    out: list[ThreadComment] = []
    for floor in result.get("comments") or []:
        items = floor.get("comment") if isinstance(floor, dict) else None
        for item in items or []:
            if not isinstance(item, dict):
                continue
            cid = str(item.get("commentid") or item.get("comment_id") or "").strip()
            if not cid:
                continue
            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            out.append(
                ThreadComment(
                    comment_id=cid,
                    user=str(user.get("username") or "").strip(),
                    text=_strip_em(str(item.get("text") or "")).strip(),
                    parent_id=str(item.get("reply_id") or "").strip(),
                    root_id=str(item.get("root_id") or "").strip(),
                    created=int(item.get("create_at") or 0),
                    up=int(item.get("up") or 0),
                    child_num=int(item.get("child_num") or 0),
                    has_more=bool(item.get("has_more")),
                    floor=int(item.get("floor_num") or 0),
                )
            )
    return out, False


async def heybox_thread_many(
    link_ids: list[str], *, cfg: Any = None, scrolls: int = 8, timeout: float = 40.0
) -> dict[str, list[ThreadComment]]:
    """一次浏览器会话，读多篇帖子的评论区。

    为什么合成一次会话：小黑盒对"短时间开很多浏览器上下文"很敏感
    （实测：密集探测之后接口直接回 show_captcha）。她要看的帖子越多，
    越应该复用同一个 page，而不是每篇开一个。

    返回 {link_id: [ThreadComment, ...]}，顺序是"根评论在前、它的预览子回复紧随"。
    实测结构（2026-10-26）：result.comments 是楼层数组，每层 {"comment": [根, 子1, 子2]}，
    根上有 child_num / has_more 表示还有更多子回复没给。
    """
    from .browser_agent import _with_page
    from .web_login import optional_state_path, state_path

    ids = [str(x).strip() for x in link_ids if str(x).strip()]
    if not ids:
        return {}

    await _session_gate(cfg)
    per_link: dict[str, list[ThreadComment]] = {i: [] for i in ids}
    current = {"id": ids[0]}
    blocked = {"hit": False}

    async def _run(page: Any) -> None:
        async def _capture(resp: Any) -> None:
            try:
                if _TREE_API_HINT not in resp.url:
                    return
                body = await resp.text()
            except Exception:  # noqa: BLE001
                return
            parsed, captcha = _parse_tree_body(body)
            if captcha:
                # 被要求人机验证必须记下来：不然调用方会以为"这篇没人评论"
                blocked["hit"] = True
                return
            if parsed:
                rows = per_link.setdefault(current["id"], [])
                seen = {c.comment_id for c in rows}
                for comment in parsed:
                    if comment.comment_id in seen:
                        continue
                    seen.add(comment.comment_id)
                    rows.append(comment)

        page.on("response", lambda r: asyncio.ensure_future(_capture(r)))
        for link_id in ids:
            current["id"] = link_id
            url = share_url_of(link_id)
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout) * 1000)
                deadline = time.time() + timeout
                while time.time() < deadline:
                    text = await page.evaluate(
                        "() => document.body ? document.body.innerText : ''"
                    )
                    if text and len(text) > 800:
                        break
                    await page.wait_for_timeout(700)
                # 先切到「最新」：她刚发的评论在最前面，比一路往下翻 9 页省事得多，
                # 也少发请求（每滚一次就是一页评论接口，翻太多最像爬虫）
                try:
                    latest = page.get_by_text("最新", exact=True).first
                    if await latest.count():
                        await latest.click(timeout=2500)
                        await page.wait_for_timeout(2000)
                except Exception:  # noqa: BLE001
                    pass
                # 少量滚动 + 随机停顿：机械地每 1.2 秒翻一屏是最容易被识别的节奏
                for _ in range(max(1, min(scrolls, 10))):
                    for locator in (await page.locator(".load-all__text").all())[:2]:
                        try:
                            await locator.click(timeout=1200)
                        except Exception:  # noqa: BLE001
                            continue
                    await page.mouse.wheel(0, 1800)
                    await page.wait_for_timeout(random.randint(2500, 5000))
            except Exception:  # noqa: BLE001 — 单篇失败不影响后面几篇
                continue

    await _with_page(
        cfg,
        _run,
        storage_state=optional_state_path("heybox", cfg),
        persist_path=str(state_path("heybox", cfg)),
    )
    if blocked["hit"] and not any(per_link.values()):
        raise HeyboxError("评论区被要求人机验证了（show_captcha），先停一会儿再看")
    return per_link


async def heybox_thread(
    ref: str, *, cfg: Any = None, limit: int = 200, timeout: float = 40.0
) -> list[ThreadComment]:
    """读单篇帖子的评论区（内部复用 heybox_thread_many）。"""
    link_id = _share_id_of(ref) or (ref or "").strip()
    data = await heybox_thread_many([link_id], cfg=cfg, timeout=timeout)
    rows = data.get(link_id) or []
    if not rows:
        raise HeyboxError("没读到评论区（被要求验证 / 帖子关了评论 / 页面改版）")
    return rows[:limit]


def _whoami(cfg: Any) -> str:
    """她自己的昵称。用来在评论区认出"哪些是说自己"的。"""
    try:
        from .web_login import read_nickname

        name = read_nickname("heybox", cfg).strip()
        if name:
            return name
    except Exception:  # noqa: BLE001
        pass
    return str(getattr(cfg, "heybox_self_nickname", "") or "").strip()


async def heybox_inbox(
    cfg: Any = None, *, limit_posts: int = 3, limit: int = 20
) -> list[dict[str, Any]]:
    """看"有没有人回我"。

    小黑盒网页版没有消息中心的路由（2026-10-26 实测：/app/message、/app/user/message
    等都会退回首页），所以走另一条稳的路：**回到自己评过的帖子，读评论区，
    找挂在我那条评论下面的回复**。

    返回每条：{"link_id", "title", "my_comment", "user", "text", "comment_id", "may_have_more"}
    """
    from .write_guard import load_state

    state = load_state(cfg)
    mine_rows = [row for row in state.get("comments", []) if str(row.get("text") or "").strip()]
    if not mine_rows:
        return []
    self_name = _whoami(cfg)
    recent = mine_rows[-max(1, limit_posts) :]
    links = [str(row.get("link_id") or "").strip() for row in recent if row.get("link_id")]
    if not links:
        return []
    # 评论按热度分页（每页 20 楼层），她刚发的评论一般排在很后面，
    # 所以这里比看单帖多翻几屏——不然会得出"没人回我"的错误结论
    threads = await heybox_thread_many(links, cfg=cfg, scrolls=16)

    found: list[dict[str, Any]] = []
    looked_but_missed = 0
    for row in recent:
        link_id = str(row.get("link_id") or "").strip()
        my_text = str(row.get("text") or "").strip()
        comments = threads.get(link_id) or []
        if not comments:
            continue
        # 认自己那条：优先"昵称 + 文案"，昵称对不上就只比文案（她改过名怎么办）
        mine = None
        for comment in comments:
            if comment.text[:16] and comment.text[:16] in my_text:
                if not self_name or comment.user == self_name:
                    mine = comment
                    break
        if mine is None:
            # 评论区分页（20 层/页），翻不到她那条就不能说"没人回你"
            looked_but_missed += 1
            continue
        # 她那条下面的回复 = 同一楼层里排在她之后的（预览子回复）
        index = comments.index(mine)
        for comment in comments[index + 1 :]:
            if comment.floor != mine.floor:
                break
            if self_name and comment.user == self_name:
                continue  # 自己说的话不算留言
            if not comment.text:
                continue
            found.append(
                {
                    "link_id": link_id,
                    "title": str(row.get("title") or ""),
                    "my_comment": my_text,
                    "user": comment.user,
                    "text": comment.text,
                    "comment_id": comment.comment_id,
                    # 楼层里还有 N 条没给全，别装作全看到了
                    "may_have_more": bool(mine.has_more) or mine.child_num > len(comments) - index - 1,
                }
            )
            if len(found) >= limit:
                return found
    if not found and looked_but_missed:
        # 明确区分"看了，没人回"和"没翻到我说过的那层"
        raise HeyboxError("评论区翻到她那条评论之前就到底了，这次没看成（不是没人回）")
    return found


async def heybox_reply(
    ref: str, target_text: str, text: str, *, cfg: Any = None, dry_run: bool = False
) -> HeyboxCommentResult:
    """在某个帖子下，回复评论区里某一条（楼中楼）。

    target_text 用"对方那条评论的开头"来定位，而不是 comment_id：
    DOM 里没有稳定的 comment_id 属性，但每条评论都带着自己的文字。

    流程和发评论一样（点入口 -> ProseMirror 打字 -> 发送），
    区别只在 STEP 1 点的是"那条评论自己的回复按钮"，不是帖子底部的评论条。
    """
    from .web_login import with_context

    raw = (ref or "").strip()
    if _HASH_RE.match(raw):
        url, link_id = share_url_of(raw), raw
    elif raw.startswith("http") and "xiaoheihe" in raw:
        url, link_id = raw, (_share_id_of(raw) or raw[-12:])
    else:
        raise HeyboxError("认不出这个帖子（要 hash 形式的分享链接）")

    key = (target_text or "").strip()[:24]
    if not key:
        raise HeyboxError("要回复哪一条？给我那条评论的开头几个字")
    cleaned = clean_comment(text, cfg)
    timeout_sec = float(getattr(cfg, "heybox_timeout", 30.0) or 30.0)

    async def _run(page: Any) -> HeyboxCommentResult:
        await page.goto(url, wait_until="domcontentloaded", timeout=int(timeout_sec) * 1000)
        deadline = time.time() + timeout_sec
        body_text = ""
        while time.time() < deadline:
            body_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            if body_text and len(body_text) > 800:
                break
            await page.wait_for_timeout(700)
        if len(body_text or "") <= 800:
            raise HeyboxError("帖子没渲染出来（被限流或验证），过十几分钟再试")

        # 找到那条评论所在的"评论条"。实测结构（2026-10-26）：
        #   div.link-comment__comment-item
        #     ├─ div.link-comment__comment-item-header   （昵称 / 赞 / 时间 / 地区）
        #     └─ div.comment-item__content-container
        #          └─ div.comment-item__content          （正文）
        # 「回复」按钮默认是**悬停才出现**的，所以要 hover 之后才找得到。
        item = None
        for _ in range(10):
            try:
                candidate = page.locator(".link-comment__comment-item").filter(has_text=key)
                if await candidate.count():
                    item = candidate.first
                    break
            except Exception:  # noqa: BLE001
                pass
            await page.mouse.wheel(0, 1700)
            await page.wait_for_timeout(random.randint(2200, 3800))
        if item is None:
            raise HeyboxError("评论区里没找到要回复的那条（可能被折叠或没翻到）")

        try:
            await item.scroll_into_view_if_needed()
            await item.hover()
            await page.wait_for_timeout(500)
        except Exception:  # noqa: BLE001
            pass

        clicked = False
        for locator in (
            item.get_by_text("回复", exact=True).first,
            item.locator("[class*='reply']").first,
            item.locator("[class*='action']").first,
        ):
            try:
                if await locator.count():
                    await locator.click(timeout=2500)
                    clicked = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not clicked:
            raise HeyboxError("没找到那条评论的「回复」按钮（悬停后才出现的那排）")

        editor = None
        for locator in (
            page.locator("div.ProseMirror.hb-editor").first,
            page.locator("[contenteditable='true']").last,
        ):
            try:
                await locator.wait_for(state="visible", timeout=8000)
                editor = locator
                break
            except Exception:  # noqa: BLE001
                continue
        if editor is None:
            raise HeyboxError("点了回复但输入框没出现")

        await editor.click()
        await page.keyboard.type(cleaned, delay=20)
        await page.wait_for_timeout(600)
        typed = (await editor.inner_text() or "").strip()
        if cleaned[:8] and cleaned[:8] not in typed:
            raise HeyboxError("字没能打进输入框")
        if dry_run:
            return HeyboxCommentResult(ok=True, text=cleaned, link_id=link_id)

        before = await page.evaluate("() => document.body ? document.body.innerText : ''")
        probe = cleaned.strip()[:24]
        sent = False
        for locator in (
            page.locator("button.link-reply__menu-btn.hb-color__btn--confirm").first,
            page.get_by_text("发送", exact=True).last,
        ):
            try:
                if await locator.count():
                    await locator.click()
                    sent = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not sent:
            raise HeyboxError("没找到发送按钮（页面大概改版了）")

        await page.wait_for_timeout(4000)
        after = await page.evaluate("() => document.body ? document.body.innerText : ''")
        try:
            editor_left = (await editor.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            editor_left = ""
        cleared = not editor_left
        appeared = (
            cleared and bool(probe) and probe in (after or "") and probe not in (before or "")
        )
        if appeared:
            return HeyboxCommentResult(ok=True, text=cleaned, link_id=link_id)
        if cleared:
            return HeyboxCommentResult(
                ok=True,
                text=cleaned,
                link_id=link_id,
                error="输入框已清空，但没回读到这条回复（可能进审核）",
            )
        raise HeyboxError("点完发送但字还在输入框里，这条没发出去")

    return await with_context("heybox", cfg, _run)
