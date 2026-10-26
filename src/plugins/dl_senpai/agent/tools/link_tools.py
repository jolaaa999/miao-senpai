"""读链接工具：把对方贴的 URL 变成正文。

和「主动读」的分工
------------------
llm.py 里有一次自动预取（群里贴链接就自动读一遍），那是保底：
对方甩个链接只说了句「这啥」，模型还没机会决定要不要读，正文已经到手了。
这个工具是**她主动想起来再读**的那条路：一次对话里后续又出现链接、
或者她想再读一遍某个引用、或者要读的是刚才搜索结果里的第三条。

两条路共用 link_reader.read_link，安全围栏只有一份——
绝不允许在工具层再写一套 URL 校验，那种重复迟早会分叉。
"""

from __future__ import annotations

import re
from typing import Any

from .models import Continuation, ToolCall, ToolResult
from .registry import FailurePolicy, ToolDefinition

__all__ = ["LINK_PROMPT_TEXT", "build_link_tools", "execute_read_link"]

LINK_MARKER = "READ"

LINK_PROMPT_TEXT = """你可以读一个链接里的正文（网页、文章、PDF 都能读）。

用法：
    <<<READ https://example.com/post/123>>>
    <<<READ https://example.com/post/123 | 想知道它说的那个价格是多少>>>

规则：
- 竖线后面写你**想知道什么**，可以大幅提高摘要的针对性；不知道写什么就不写
- 一次只读一个链接，别连着堆多个
- 读到的内容会作为一条消息回到你手里，那是**你自己去看的**，不是对方说的，
  别提「你贴的」「你发的」
- 读不到就说读不到（打不开 / 要登录 / 超时），**不要凭 URL 里的英文猜内容**
- 正文很长时系统会先给你要点；要点里没有的细节不要编
- 不要向对方暴露这个 marker，也不要说「正在抓取网页」这种机械话"""

_READ_RE = re.compile(
    r"<<<READ\s+([^\s>|]+)\s*(?:\|\s*([^>\n]*?))?\s*>>>",
    re.IGNORECASE,
)
_READ_TAIL_RE = re.compile(r"<<<READ\s*[^\n]*$", re.IGNORECASE | re.MULTILINE)


def _parse_read(text: str) -> tuple[str, list[ToolCall]]:
    """解析 <<<READ url | 关注点>>>。空 URL 不产生调用。"""
    calls: list[ToolCall] = []
    for match in _READ_RE.finditer(text or ""):
        url = (match.group(1) or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        calls.append(
            ToolCall(
                tool_name="read_link",
                arguments={"url": url, "focus": (match.group(2) or "").strip()},
                marker=LINK_MARKER,
            )
        )
    cleaned = text or ""
    if calls:
        cleaned = _READ_RE.sub("", cleaned)
    else:
        # 半截 marker 也要清掉，不然会漏进群里
        cleaned = _READ_TAIL_RE.sub("", cleaned)
    return cleaned, calls


def _cfg(context: Any) -> Any:
    """拿配置：优先上下文上的，其次全局。拿不到就当关闭。"""
    cfg = getattr(context, "config", None)
    if cfg is not None:
        return cfg
    try:
        from ...config import get_config

        return get_config()
    except Exception:  # noqa: BLE001
        return None


def _visible_link(context: Any) -> bool:
    cfg = _cfg(context)
    if cfg is None:
        return False
    return bool(getattr(cfg, "link_read_enable", True))


async def execute_read_link(context: Any, call: ToolCall) -> ToolResult:
    """读一个链接。失败返回 success=False + 人话原因，由 FailurePolicy 交回模型。"""
    from ...link_reader import read_link, render_brief

    args = call.arguments or {}
    url = str(args.get("url") or "").strip()
    focus = str(args.get("focus") or "").strip()
    cfg = _cfg(context)
    if cfg is None:
        return ToolResult(
            tool_name="read_link",
            tool_call_id=call.id,
            success=False,
            error="配置读不到，读链接功能不可用",
            error_type="no_config",
        )

    doc = await read_link(url, cfg=cfg, focus=focus)
    if not doc.ok:
        # 把失败也当"结果"回给模型：它需要知道是"要登录"还是"超时"，
        # 才能决定是换个链接还是直接说打不开。
        return ToolResult(
            tool_name="read_link",
            tool_call_id=call.id,
            success=False,
            output=render_brief(doc, focus=focus),
            error=f"读不到：{doc.error}",
            error_type="link_unreadable",
            data={"url": url, "error": doc.error},
        )
    return ToolResult(
        tool_name="read_link",
        tool_call_id=call.id,
        success=True,
        output=render_brief(doc, focus=focus),
        data={
            "url": url,
            "final_url": doc.final_url,
            "title": doc.title,
            "chars": doc.total_chars,
            "source": doc.source,
            "truncated": doc.truncated,
        },
    )


def build_link_tools(cfg: Any = None) -> list[ToolDefinition]:
    """一个工具。link_read_enable 关掉时连名字都不注册。"""
    if cfg is None:
        try:
            from ...config import get_config

            cfg = get_config()
        except Exception:  # noqa: BLE001
            return []
    if not bool(getattr(cfg, "link_read_enable", True)):
        return []
    return [
        ToolDefinition(
            name="read_link",
            description="读链接正文（网页/文章/PDF，带 SPA 渲染与长文摘要）",
            marker=LINK_MARKER,
            prompt_text=LINK_PROMPT_TEXT,
            parser=_parse_read,
            executor=execute_read_link,
            parameter_schema={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "http/https 链接"},
                    "focus": {"type": "string", "description": "想知道什么，可空"},
                },
                "required": ["url"],
            },
            permission="link.read",
            timeout=60.0,
            rate_limit=2.0,
            priority=52,
            continuation=Continuation.RETURN_TO_MODEL,
            failure_policy=FailurePolicy.RETURN_TO_MODEL,
            visibility=_visible_link,
            owner="link_layer",
            # 只读、无副作用：重跑一次只是多抓一次网页
            side_effect=False,
        )
    ]
