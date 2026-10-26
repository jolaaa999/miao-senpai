"""相册工具：她自己收藏一张图、翻一眼自己的相册。

为什么是「工具」而不是自动收集
------------------------------
自动把她看到的所有图都存下来，那是缓存，不是相册。
原版的观察是：**意义在「她挑」**——一百张里她自己挑三张，那三张才是她的。
所以这里只给她两个动作：收藏（必须带理由）、看看自己收过什么，
**没有删除**：删是管理员干的活。

安全性
------
收藏的源文件必须落在她自己的工作目录（生成图 / 沙箱）里，
判定写在 gallery._resolve_source。这样这个工具就不可能被用来
「把系统里任意文件复制进数据目录再发出去」——那等于绕过文件工具的沙箱。
"""

from __future__ import annotations

import re
from typing import Any

from ...gallery import keep_picture, list_items, parse_keep_marker
from .models import Continuation, ToolCall, ToolResult
from .registry import FailurePolicy, ToolDefinition

__all__ = ["build_gallery_tools", "execute_gallery_keep", "execute_gallery_list"]

KEEP_MARKER = "KEEP"
MY_PICS_MARKER = "MY_PICS"

GALLERY_PROMPT_TEXT = """你可以把「你想留下来的图」收进自己的相册，并写一句为什么。

收藏（必须带理由，理由不超过 40 字；路径只能是生成图/工作区里的文件）：
    <<<KEEP data/dl_senpai/draws/2026-10-20-a1b2.png | 他挑的那张，跪坐撅嘴>>
    <<<KEEP data/dl_senpai/draws/xxx.png>>>

看看自己收过什么：
    <<<MY_PICS>>>

什么时候用：你刚画完一张自己觉得好的、或者他发来的图你确实喜欢，
才收。**不要为了「有内容」而收**——相册里全是随手存的图，就没有意义了。
收完不用向对方汇报，那是你自己的事。"""


def _visible_gallery(context: Any) -> bool:
    cfg = getattr(context, "config", None)
    if cfg is None:
        return False
    return bool(getattr(cfg, "gallery_enable", False))


def _cfg(context: Any) -> Any:
    return getattr(context, "config", None)


def _parse_keep(text: str) -> tuple[str, list[ToolCall]]:
    cleaned, requests = parse_keep_marker(text or "")
    calls = [
        ToolCall(
            tool_name="gallery_keep",
            arguments={"path": item["path"], "reason": item["reason"]},
            marker=KEEP_MARKER,
        )
        for item in requests
    ]
    if not calls:
        # 半截 marker（模型写崩了）也要清掉，不然会漏进聊天
        cleaned = re.sub(r"<<<KEEP[^\n]*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned, calls


def _parse_my_pics(text: str) -> tuple[str, list[ToolCall]]:
    pattern = re.compile(r"<<<MY_PICS\s*>>>", re.IGNORECASE)
    calls = [ToolCall(tool_name="gallery_list", marker=MY_PICS_MARKER) for _ in pattern.finditer(text or "")]
    cleaned = pattern.sub("", text or "") if calls else (text or "")
    return cleaned, calls


async def execute_gallery_keep(context: Any, call: ToolCall) -> ToolResult:
    cfg = _cfg(context)
    path = str((call.arguments or {}).get("path") or "")
    reason = str((call.arguments or {}).get("reason") or "")
    item = keep_picture(cfg, path, reason, actor=str(getattr(context, "actor", "") or ""))
    if item is None:
        return ToolResult(
            tool_name="gallery_keep",
            tool_call_id=call.id,
            success=False,
            error=f"没收进来：路径不在你能收藏的目录里，或者文件不存在（{path}）",
            error_type="gallery_rejected",
        )
    return ToolResult(
        tool_name="gallery_keep",
        tool_call_id=call.id,
        success=True,
        output=f"收进相册了：{item.reason or '（没写理由）'}",
        data={"path": item.path, "reason": item.reason},
    )


async def execute_gallery_list(context: Any, call: ToolCall) -> ToolResult:
    cfg = _cfg(context)
    items = list_items(cfg, 8)
    if not items:
        return ToolResult(
            tool_name="gallery_list",
            tool_call_id=call.id,
            success=True,
            output="相册还是空的。",
        )
    lines = [f"- {item.reason or '（没写理由）'}" for item in items]
    return ToolResult(
        tool_name="gallery_list",
        tool_call_id=call.id,
        success=True,
        output="你自己收过的：\n" + "\n".join(lines),
        data={"count": len(items)},
    )


def build_gallery_tools(cfg: Any = None) -> list[ToolDefinition]:
    """两个工具。未开启 gallery_enable 时返回空列表（连名字都不注册）。"""
    if cfg is None:
        try:
            from ...config import get_config

            cfg = get_config()
        except Exception:  # noqa: BLE001
            return []
    if not bool(getattr(cfg, "gallery_enable", False)):
        return []
    common = dict(
        continuation=Continuation.RETURN_TO_MODEL,
        failure_policy=FailurePolicy.RETURN_TO_MODEL,
        visibility=_visible_gallery,
        owner="gallery_layer",
        prompt_text=GALLERY_PROMPT_TEXT,
    )
    return [
        ToolDefinition(
            name="gallery_keep",
            description="把一张图收进她自己的相册，并写一句理由",
            marker=KEEP_MARKER,
            parser=_parse_keep,
            executor=execute_gallery_keep,
            parameter_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "生成图/工作区里的文件路径"},
                    "reason": {"type": "string", "description": "为什么留下它，40 字以内"},
                },
                "required": ["path"],
            },
            permission="gallery.keep",
            timeout=15.0,
            rate_limit=0.1,
            priority=48,
            side_effect=True,
            **common,
        ),
        ToolDefinition(
            name="gallery_list",
            description="看看她自己收藏过什么",
            marker=MY_PICS_MARKER,
            parser=_parse_my_pics,
            executor=execute_gallery_list,
            parameter_schema={"type": "object", "properties": {}},
            permission="gallery.list",
            timeout=10.0,
            rate_limit=0.1,
            priority=49,
            side_effect=False,
            **common,
        ),
    ]
