"""B站 / 小黑盒只读工具：刷视频、搜帖子。

为什么两个工具分开而不是合成一个 WEB 大工具
--------------------------------------------
两者的失败原因完全不同：B站失败是「接口风控/没这个视频」，
小黑盒失败是「要开浏览器、页面改版、被限流」。合成一个工具，
模型看到的错误会糊成一团，它就没法决定接下来怎么办
（该不该换个关键词、该不该放弃）。

共同点只有三条，都做在这一层：每日配额、失败退配额、
输出里必须写清「你看到的是文字，没看过画面」。
"""

from __future__ import annotations

import re
from typing import Any

from .models import Continuation, ToolCall, ToolResult
from .registry import FailurePolicy, ToolDefinition

__all__ = ["build_web_extra_tools", "execute_bili", "execute_heybox"]

BILI_MARKER = "BILI"
BILIMEMO_MARKER = "BILIMEMO"
HEYBOX_MARKER = "HEYBOX"

BILI_PROMPT_TEXT = """你可以自己上 B 站看东西，也能看到「谁回了我」。

用法：
    <<<BILI hot>>>
    <<<BILI hot 20>>>
    <<<BILI search 怪物猎人 太刀配装>>>
    <<<BILI video BV1xx411c7mD>>>
    <<<BILI inbox>>>                  # 看有没有人回我在B站的评论

说明：
- 用 inbox 看到有人回你，想接话就用 BILIPOST 的 reply（只有主人点头或你自己确实有话说才用）
- hot 是首页综合热门；search 是搜视频；video 是看某一个视频的标题/简介/热评
- 想聊某个视频的具体内容，先 search 找到 BV 号，再 video 看那条
- 你拿到的是**文字**：标题、简介、热评。你没看画面也没听声音，
  不要描述视频里出现了什么、说了什么、弹幕怎么飘
- 一次只看一条，别连着刷十个视频；别人的问题跟你刷 B 站无关就别刷
- 不要暴露 marker，也不要说「正在调用接口」这种机械话"""

BILIMEMO_PROMPT_TEXT = """你有一本「B站刷到过、真记住了」的小本子。

用法：
    <<<BILIMEMO>>>                # 最近记住的几条
    <<<BILIMEMO 5>>>              # 最近 5 条
    <<<BILIMEMO 买车>>>            # 按主题搜

说明：
- 主人问「你最近刷到啥」「你记得那个XX吗」就查这里，别凭空编
- 记住的是**看过标题和文字**的东西，不是看过画面：别说「我看了那个视频，拍得真好」
- 本子里没有就说没有，别为了接话硬凑"""

HEYBOX_PROMPT_TEXT = """你可以自己上小黑盒逛、搜、看帖子（只读）。

用法：
    <<<HEYBOX feed>>>                 # 刷首页信息流，随便看看有什么
    <<<HEYBOX inbox>>>                # 看有没有人回我之前评的话
    <<<HEYBOX 大肥鱼 部署>>>           # 站内搜索
    <<<HEYBOX read 8e7f729adcb3>>>    # 读某篇正文

说明：
- **没人搭话、你自己想知道点什么的时候，可以 feed 刷一下**，像人无聊时刷手机；
  刷到感兴趣的顺着 read 看正文，看完再决定要不要说点什么
- 刷到什么就说什么，别硬凑：十条里只对一条有兴趣，就只聊那一条
- 搜索/刷流给的只是标题和摘要，不代表你看过内容；别拿标题当正文聊
- 打不开就说打不开（可能要登录 / 被限流 / 帖子删了），不要凭标题编
- 一次搜一个词，别把整个提问塞进去
- 想评论要另外用 HEYPOST（只有主人点头或你自己确实有话说才用）
- 不要暴露 marker，也不要说「正在抓取」这种机械话"""

# hot 后面可以不带数字；search/video 后面的整行都是参数
_BILI_RE = re.compile(
    r"<<<BILI(?:\s+(hot|search|video|inbox))?(?:\s+([^>\n]*?))?\s*(?:>>>|＞＞＞)",
    re.IGNORECASE,
)
_BILI_TAIL_RE = re.compile(r"<<<BILI\s*[^\n]*$", re.IGNORECASE | re.MULTILINE)
_HEYBOX_RE = re.compile(
    r"<<<HEYBOX\s+([^>\n]+?)\s*(?:>>>|＞＞＞)", re.IGNORECASE
)
_HEYBOX_TAIL_RE = re.compile(r"<<<HEYBOX\s*[^\n]*$", re.IGNORECASE | re.MULTILINE)


def _cfg(context: Any) -> Any:
    cfg = getattr(context, "config", None)
    if cfg is not None:
        return cfg
    try:
        from ...config import get_config

        return get_config()
    except Exception:  # noqa: BLE001
        return None


def _visible_bili(context: Any) -> bool:
    cfg = _cfg(context)
    return bool(getattr(cfg, "bili_enable", True)) if cfg is not None else False


def _visible_heybox(context: Any) -> bool:
    cfg = _cfg(context)
    return bool(getattr(cfg, "heybox_enable", True)) if cfg is not None else False


_BILIMEMO_RE = re.compile(r"<<<BILIMEMO(?:\s+([^>\n]*?))?\s*(?:>>>|＞＞＞)", re.IGNORECASE)


def _parse_bilimemo(text: str) -> tuple[str, list[ToolCall]]:
    """解析 <<<BILIMEMO ...>>>。不写参数就是「最近记住的」。"""
    calls: list[ToolCall] = []
    for match in _BILIMEMO_RE.finditer(text or ""):
        argument = (match.group(1) or "").strip()
        if not argument:
            calls.append(
                ToolCall(
                    tool_name="bili_memo",
                    arguments={"action": "recent", "limit": 6},
                    marker=BILIMEMO_MARKER,
                )
            )
        elif argument.isdigit():
            calls.append(
                ToolCall(
                    tool_name="bili_memo",
                    arguments={"action": "recent", "limit": max(1, min(int(argument), 20))},
                    marker=BILIMEMO_MARKER,
                )
            )
        else:
            calls.append(
                ToolCall(
                    tool_name="bili_memo",
                    arguments={"action": "search", "query": argument, "limit": 6},
                    marker=BILIMEMO_MARKER,
                )
            )
    if calls:
        stripped = _BILIMEMO_RE.sub("", text or "")
        return stripped, calls
    return text or "", calls


async def execute_bili_memo(context: Any, call: ToolCall) -> ToolResult:
    """查她自己那本「刷到过、记住了」的小本子。纯读本地文件，不碰网络。"""
    from ... import bili_memory as bmemo

    args = call.arguments or {}
    cfg = _cfg(context)
    if cfg is None:
        return ToolResult(
            tool_name="bili_memo",
            tool_call_id=call.id,
            success=False,
            error="配置读不到",
            error_type="no_config",
        )
    query = str(args.get("query") or "").strip()
    limit = int(args.get("limit") or 6)
    rows = bmemo.search(cfg, query, limit=limit) if query else bmemo.search(cfg, "", limit=limit)
    return ToolResult(
        tool_name="bili_memo",
        tool_call_id=call.id,
        success=True,
        output=bmemo.render(rows, limit=limit),
        data={"count": len(rows), "query": query},
    )


def _parse_bili(text: str) -> tuple[str, list[ToolCall]]:
    """解析 <<<BILI ...>>>。没写子命令时按 hot 处理。"""
    calls: list[ToolCall] = []
    for match in _BILI_RE.finditer(text or ""):
        action = (match.group(1) or "hot").strip().lower()
        argument = (match.group(2) or "").strip()
        if action == "inbox":
            # <<<BILI inbox>>>：看「回复我的」
            calls.append(
                ToolCall(
                    tool_name="bili",
                    arguments={"action": "inbox"},
                    marker=BILI_MARKER,
                )
            )
        elif action == "hot":
            limit = 10
            if argument.isdigit():
                limit = max(1, min(int(argument), 20))
            calls.append(
                ToolCall(
                    tool_name="bili",
                    arguments={"action": "hot", "limit": limit},
                    marker=BILI_MARKER,
                )
            )
        elif action == "search":
            if not argument:
                continue
            calls.append(
                ToolCall(
                    tool_name="bili",
                    arguments={"action": "search", "keyword": argument, "limit": 8},
                    marker=BILI_MARKER,
                )
            )
        elif action == "video":
            if not argument:
                continue
            calls.append(
                ToolCall(
                    tool_name="bili",
                    arguments={"action": "video", "ref": argument, "limit": 5},
                    marker=BILI_MARKER,
                )
            )
    cleaned = text or ""
    if calls:
        cleaned = _BILI_RE.sub("", cleaned)
    else:
        cleaned = _BILI_TAIL_RE.sub("", cleaned)
    return cleaned, calls


def _parse_heybox(text: str) -> tuple[str, list[ToolCall]]:
    """解析 <<<HEYBOX 关键词>>> 与 <<<HEYBOX read hash>>>。"""
    calls: list[ToolCall] = []
    for match in _HEYBOX_RE.finditer(text or ""):
        argument = (match.group(1) or "").strip()
        if not argument:
            continue
        lowered = argument.lower()
        if lowered in ("inbox", "留言", "消息"):
            # <<<HEYBOX inbox>>>：看有没有人回我
            calls.append(
                ToolCall(
                    tool_name="heybox",
                    arguments={"action": "inbox"},
                    marker=HEYBOX_MARKER,
                )
            )
            continue
        if lowered == "feed" or lowered.startswith("feed "):
            # <<<HEYBOX feed>>> / <<<HEYBOX feed 20>>>：让她随便逛逛信息流
            tail = argument[len("feed"):].strip()
            limit = 12
            if tail.isdigit():
                limit = max(1, min(30, int(tail)))
            calls.append(
                ToolCall(
                    tool_name="heybox",
                    arguments={"action": "feed", "limit": limit},
                    marker=HEYBOX_MARKER,
                )
            )
            continue
        if lowered.startswith("read"):
            ref = argument[len("read"):].strip()
            if not ref:
                continue
            calls.append(
                ToolCall(
                    tool_name="heybox",
                    arguments={"action": "read", "ref": ref},
                    marker=HEYBOX_MARKER,
                )
            )
        else:
            calls.append(
                ToolCall(
                    tool_name="heybox",
                    arguments={"action": "search", "keyword": argument, "limit": 6},
                    marker=HEYBOX_MARKER,
                )
            )
    cleaned = text or ""
    if calls:
        cleaned = _HEYBOX_RE.sub("", cleaned)
    else:
        cleaned = _HEYBOX_TAIL_RE.sub("", cleaned)
    return cleaned, calls


async def execute_bili(context: Any, call: ToolCall) -> ToolResult:
    """执行 B 站的三种动作。配额先扣再用，失败退回。"""
    from ... import bili as bili_mod
    from ...web_login import WebLoginError
    from ...quota import check_quota, refund_quota

    args = call.arguments or {}
    action = str(args.get("action") or "hot").lower()
    cfg = _cfg(context)
    if cfg is None:
        return ToolResult(
            tool_name="bili",
            tool_call_id=call.id,
            success=False,
            error="配置读不到，B站功能不可用",
            error_type="no_config",
        )

    allowed, used = await check_quota("bili", int(getattr(cfg, "bili_daily_limit", 60) or 0), cfg)
    if not allowed:
        return ToolResult(
            tool_name="bili",
            tool_call_id=call.id,
            success=False,
            output=f"【系统】今天的 B 站额度用完了（{used} 次）。这次没去看。",
            error="bili_daily_limit",
            error_type="quota_exceeded",
        )

    try:
        if action == "inbox":
            try:
                rows = await bili_mod.bili_messages(cfg=cfg, limit=20)
            except (bili_mod.BiliError, WebLoginError) as exc:
                return ToolResult(
                    tool_name="bili",
                    tool_call_id=call.id,
                    success=False,
                    output=f"【系统】看不到B站留言：{exc}",
                    error=str(exc),
                    error_type="inbox_failed",
                )
            return ToolResult(
                tool_name="bili",
                tool_call_id=call.id,
                success=True,
                output=bili_mod.render_messages(rows, limit=6),
                data={"count": len(rows)},
            )

        if action == "hot":
            items = await bili_mod.bili_hot(int(args.get("limit") or 10), cfg=cfg)
            return ToolResult(
                tool_name="bili",
                tool_call_id=call.id,
                success=bool(items),
                output=bili_mod.render_hot(items, limit=int(args.get("limit") or 10)),
                data={"action": "hot", "count": len(items)},
                error="" if items else "热门榜单是空的",
                error_type="" if items else "empty",
            )
        if action == "search":
            keyword = str(args.get("keyword") or "").strip()
            items = await bili_mod.bili_search(keyword, int(args.get("limit") or 8), cfg=cfg)
            return ToolResult(
                tool_name="bili",
                tool_call_id=call.id,
                success=bool(items),
                output=bili_mod.render_search(
                    items, keyword=keyword, limit=int(args.get("limit") or 8)
                ),
                data={"action": "search", "keyword": keyword, "count": len(items)},
                error="" if items else "一条都没搜到",
                error_type="" if items else "empty",
            )
        if action == "video":
            video = await bili_mod.bili_video(
                str(args.get("ref") or ""),
                comment_limit=int(args.get("limit") or 5),
                cfg=cfg,
            )
            return ToolResult(
                tool_name="bili",
                tool_call_id=call.id,
                success=True,
                output=bili_mod.render_video(video),
                data={
                    "action": "video",
                    "bvid": video.bvid,
                    "title": video.title,
                    "comments": len(video.comments),
                },
            )
        return ToolResult(
            tool_name="bili",
            tool_call_id=call.id,
            success=False,
            error=f"不认识的 BILI 子命令：{action}",
            error_type="bad_action",
        )
    except bili_mod.BiliError as exc:
        # 接口没给东西：把额度退回去，别让她因为超时白扣次数
        await refund_quota("bili", cfg)
        return ToolResult(
            tool_name="bili",
            tool_call_id=call.id,
            success=False,
            output=f"【系统】B 站这次没看成：{exc}",
            error=str(exc),
            error_type="bili_error",
        )


async def execute_heybox(context: Any, call: ToolCall) -> ToolResult:
    """搜索或读帖。读帖走 link_reader，失败话术也由它给。"""
    from ... import heybox as hs_mod
    from ...web_login import WebLoginError
    from ...quota import check_quota, refund_quota

    args = call.arguments or {}
    action = str(args.get("action") or "search").lower()
    cfg = _cfg(context)
    if cfg is None:
        return ToolResult(
            tool_name="heybox",
            tool_call_id=call.id,
            success=False,
            error="配置读不到，小黑盒功能不可用",
            error_type="no_config",
        )

    allowed, used = await check_quota(
        "heybox", int(getattr(cfg, "heybox_daily_limit", 40) or 0), cfg
    )
    if not allowed:
        return ToolResult(
            tool_name="heybox",
            tool_call_id=call.id,
            success=False,
            output=f"【系统】今天的小黑盒额度用完了（{used} 次）。这次没去看。",
            error="heybox_daily_limit",
            error_type="quota_exceeded",
        )

    if action == "inbox":
        try:
            rows = await hs_mod.heybox_inbox(
                cfg, limit_posts=int(getattr(cfg, "heybox_inbox_posts", 1) or 1)
            )
        except (hs_mod.HeyboxError, WebLoginError) as exc:
            return ToolResult(
                tool_name="heybox",
                tool_call_id=call.id,
                success=False,
                output=f"【系统】看不到留言：{exc}",
                error=str(exc),
                error_type="inbox_failed",
            )
        if not rows:
            return ToolResult(
                tool_name="heybox",
                tool_call_id=call.id,
                success=True,
                output="评论区暂时没有回你的留言。",
                data={"count": 0},
            )
        lines = [f"有人回你（{len(rows)} 条）："]
        for row in rows:
            lines.append(
                f"- {row['user'] or '某人'}：{row['text'][:60]}"
                + (f"（在《{row['title'][:20]}》下）" if row.get("title") else "")
            )
            lines.append(
                f"  回这条：<<<HEYPOST reply {row['link_id']}|{row['text'][:12]}|<你要说的话>>>"
            )
        return ToolResult(
            tool_name="heybox",
            tool_call_id=call.id,
            success=True,
            output="\n".join(lines),
            data={"count": len(rows)},
        )

    if action == "feed":
        allowed, used = await check_quota("heybox", int(getattr(cfg, "heybox_daily_limit", 40) or 0), cfg)
        if not allowed:
            return ToolResult(
                tool_name="heybox",
                tool_call_id=call.id,
                success=False,
                output=f"【系统】今天刷小黑盒的次数用完了（{used} 次）。",
                error="heybox_daily_limit",
                error_type="quota_exceeded",
            )
        try:
            posts = await hs_mod.heybox_feed(
                cfg=cfg, limit=int(args.get("limit") or 12)
            )
        except (hs_mod.HeyboxError, WebLoginError) as exc:
            await refund_quota("heybox", cfg)
            return ToolResult(
                tool_name="heybox",
                tool_call_id=call.id,
                success=False,
                output=f"【系统】刷不出来：{exc}",
                error=str(exc),
                error_type="feed_failed",
            )
        return ToolResult(
            tool_name="heybox",
            tool_call_id=call.id,
            success=True,
            output=hs_mod.render_feed(posts, limit=8),
            data={"count": len(posts), "links": [p.link_id for p in posts]},
        )

    if action == "read":
        from ...link_reader import render_brief

        try:
            doc = await hs_mod.heybox_read(
                str(args.get("ref") or ""),
                cfg=cfg,
                focus=str(args.get("focus") or ""),
            )
        except hs_mod.HeyboxError as exc:
            await refund_quota("heybox", cfg)
            return ToolResult(
                tool_name="heybox",
                tool_call_id=call.id,
                success=False,
                output=f"【系统读小黑盒失败】{exc}",
                error=str(exc),
                error_type="heybox_error",
            )
        if not doc.ok:
            return ToolResult(
                tool_name="heybox",
                tool_call_id=call.id,
                success=False,
                output=render_brief(doc),
                error=f"读不到：{doc.error}",
                error_type="unreadable",
            )
        return ToolResult(
            tool_name="heybox",
            tool_call_id=call.id,
            success=True,
            output=render_brief(doc),
            data={
                "action": "read",
                "title": doc.title,
                "chars": doc.total_chars,
                "source": doc.source,
            },
        )

    keyword = str(args.get("keyword") or "").strip()
    try:
        result = await hs_mod.heybox_search(
            keyword, int(args.get("limit") or 6), cfg=cfg
        )
    except hs_mod.HeyboxError as exc:
        await refund_quota("heybox", cfg)
        return ToolResult(
            tool_name="heybox",
            tool_call_id=call.id,
            success=False,
            output=f"【系统】小黑盒这次没搜成：{exc}",
            error=str(exc),
            error_type="heybox_error",
        )
    return ToolResult(
        tool_name="heybox",
        tool_call_id=call.id,
        success=bool(result.posts),
        output=hs_mod.render_heybox_search(result),
        data={
            "action": "search",
            "keyword": keyword,
            "count": len(result.posts),
        },
        error="" if result.posts else "没搜到",
        error_type="" if result.posts else "empty",
    )


def build_web_extra_tools(cfg: Any = None) -> list[ToolDefinition]:
    """两个只读工具。各自 enable 关掉时连名字都不注册。"""
    if cfg is None:
        try:
            from ...config import get_config

            cfg = get_config()
        except Exception:  # noqa: BLE001
            return []
    tools: list[ToolDefinition] = []
    if bool(getattr(cfg, "bili_enable", True)):
        tools.append(
            ToolDefinition(
                name="bili",
                description="刷 B站：首页热门、搜视频、看单个视频的简介与热评（只读）",
                marker=BILI_MARKER,
                prompt_text=BILI_PROMPT_TEXT,
                parser=_parse_bili,
                executor=execute_bili,
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["hot", "search", "video", "inbox"],
                            "description": "hot=热门 search=搜索 video=看某条",
                        },
                        "keyword": {"type": "string", "description": "搜索词"},
                        "ref": {"type": "string", "description": "BV 号 / av 号 / 视频链接"},
                        "limit": {"type": "integer", "description": "条数或评论条数"},
                    },
                    "required": ["action"],
                },
                permission="bili.read",
                timeout=40.0,
                rate_limit=3.0,
                priority=53,
                continuation=Continuation.RETURN_TO_MODEL,
                failure_policy=FailurePolicy.RETURN_TO_MODEL,
                visibility=_visible_bili,
                owner="social_layer",
                side_effect=False,
            )
        )
    if bool(getattr(cfg, "bili_enable", True)):
        tools.append(
            ToolDefinition(
                name="bili_memo",
                description="查她自己记住的B站内容（本地小本子，只读）",
                marker=BILIMEMO_MARKER,
                prompt_text=BILIMEMO_PROMPT_TEXT,
                parser=_parse_bilimemo,
                executor=execute_bili_memo,
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["recent", "search"]},
                        "query": {"type": "string", "description": "主题关键词"},
                        "limit": {"type": "integer", "description": "条数"},
                    },
                    "required": ["action"],
                },
                permission="bili.memo",
                timeout=10.0,
                rate_limit=1.0,
                priority=54,
                continuation=Continuation.RETURN_TO_MODEL,
                failure_policy=FailurePolicy.RETURN_TO_MODEL,
                owner="social_layer",
                side_effect=False,
            )
        )
    if bool(getattr(cfg, "heybox_enable", True)):
        tools.append(
            ToolDefinition(
                name="heybox",
                description="小黑盒站内搜索与读帖（只读）",
                marker=HEYBOX_MARKER,
                prompt_text=HEYBOX_PROMPT_TEXT,
                parser=_parse_heybox,
                executor=execute_heybox,
                parameter_schema={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["search", "read"],
                            "description": "search=搜帖子 read=读某篇",
                        },
                        "keyword": {"type": "string", "description": "搜索词"},
                        "ref": {"type": "string", "description": "分享链接里的 hash"},
                        "focus": {"type": "string", "description": "想知道什么"},
                        "limit": {"type": "integer", "description": "条数"},
                    },
                    "required": ["action"],
                },
                permission="heybox.read",
                timeout=90.0,
                rate_limit=2.5,
                priority=54,
                continuation=Continuation.RETURN_TO_MODEL,
                failure_policy=FailurePolicy.RETURN_TO_MODEL,
                visibility=_visible_heybox,
                owner="social_layer",
                side_effect=False,
            )
        )
    return tools
