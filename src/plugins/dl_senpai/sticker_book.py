"""表情本子：她自己翻、自己写备注、自己把某条消息里的表情收进来。

为什么需要它（2026-10-27）：

学姐本来就会**自动**收集群里出现的表情包（观察者路径），也能按关键词挑一张
发出去。但库是黑盒：几百张图她看不见有哪几张、更没法给某一张写一句「这张是
阴阳人的」——于是「挑表情」永远停留在关键词碰运气，和 qq-bridge 里
list_stickers / collect_sticker / set_sticker_remark / sticker_note 那套
「有目录、有备注、能手动收」完全不是一回事。

四个动作（marker 用 EMOJI，不能用 STICKER：那个正则会把 STICKERBOOK 当成
STICKER 的参数吞掉）：

    <<<EMOJI>>>                    看本子最近收了哪些（编号从最新的算 1）
    <<<EMOJI 3>>>                  把第 3 张发出来看看
    <<<EMOJI NOTE 3 阴阳人|#阴阳 #无语>>>   写备注 + 追加标签
    <<<EMOJI COLLECT 123456>>>     把这条消息里的表情手动收进来

设计取舍：编号只按「最新的算第 1 张」。要是按入库顺序编号，她算不清——她看
不到那个数字，只能靠列表。列表里的顺序和发送时用的编号必须是同一个顺序。
"""

from __future__ import annotations

import re
import time
from types import SimpleNamespace
from typing import Any

from .agent.tools.models import Continuation, ToolCall, ToolResult
from .agent.tools.registry import FailurePolicy, ToolDefinition
from .observability.logging import log_event

EMOJI_MARKER = "EMOJI"

_MARKER_RE = re.compile(r"<<<\s*EMOJI\s*([^>\n]*)>>>", re.IGNORECASE)
# 备注里用竖线分开备注与标签：「阴阳人|#阴阳 #无语」
_NOTE_SPLIT_RE = re.compile(r"[|｜]")
_TAG_RE = re.compile(r"#([\w\u4e00-\u9fff]{1,8})")

EMOJI_PROMPT_TEXT = """【表情本子】
你有一本自己的表情包本子——群里出现过的表情会自动收进去，你也可以手动收、
自己写备注。看不见有哪些，就先翻一眼：

    <<<EMOJI>>>

想看看某一张长什么样：<<<EMOJI 3>>>
给它写一句备注（外加标签）：<<<EMOJI NOTE 3 阴阳人|#阴阳 #无语>>>
把某条消息里的表情收进来：<<<EMOJI COLLECT 消息ID>>>

什么时候用：你想发一张「说不清只能用图」的表情，但按关键词挑不准；
或者某张图你用过好几次、想记下它到底什么调性。

什么时候别用：正常接话、已经有合适的图可发。翻本子不发图就是纯浪费时间。
备注写字数少一点，是给你自己看的：这张图什么语气、什么场合用。
"""


def parse_emoji_request(raw_reply: str) -> tuple[str, list[ToolCall]]:
    """解析 <<<EMOJI ...>>>：空 = 列表；数字 = 看图；NOTE/COLLECT = 具体动作。"""
    text = raw_reply or ""
    calls: list[ToolCall] = []
    for match in _MARKER_RE.finditer(text):
        body = (match.group(1) or "").strip()
        args: dict[str, Any] = {"op": "list", "ref": "", "note": "", "tags": []}
        if not body:
            args["op"] = "list"
        elif body.isdigit():
            args["op"] = "show"
            args["ref"] = body
        else:
            upper = body.upper()
            if upper.startswith("NOTE "):
                rest = body[5:].strip()
                ref, _, payload = rest.partition(" ")
                payload = payload.strip()
                note, tags = _split_note(payload)
                args.update({"op": "note", "ref": ref.strip(), "note": note, "tags": tags})
            elif upper.startswith("COLLECT "):
                args.update({"op": "collect", "ref": body[8:].strip()})
            else:
                # 兜底：当成「按关键词看一张」，直接走列表里最匹配的那张
                args.update({"op": "show", "ref": body.strip()})
        calls.append(
            ToolCall(
                tool_name="emoji",
                arguments=args,
                raw=match.group(0),
                marker=EMOJI_MARKER,
            )
        )
    return _MARKER_RE.sub("", text), calls


def _split_note(payload: str) -> tuple[str, list[str]]:
    """把「阴阳人|#阴阳 #无语」拆成 (备注, 标签列表)。"""
    parts = [p.strip() for p in _NOTE_SPLIT_RE.split(payload) if p.strip()]
    note = parts[0] if parts else ""
    tags: list[str] = []
    for chunk in parts[1:] + [note]:
        tags.extend(_TAG_RE.findall(chunk))
    # 标签不该留在备注正文里，否则「阴阳人|」这种尾巴会显得很怪
    note = _TAG_RE.sub("", note).strip()
    return note, list(dict.fromkeys(tags))[:8]


def _describe(record: Any) -> str:
    """一行描述：备注 > 关键词 > 上下文。全都空就说「没备注」。"""
    bits: list[str] = []
    if record.note:
        bits.append(record.note)
    if record.tags:
        bits.append(" ".join(f"#{t}" for t in record.tags))
    if not bits:
        bits.append(record.context_text or " ".join(record.keywords[:4]) or "没备注")
    return " | ".join(bits)


async def execute_emoji(context: Any, call: ToolCall) -> ToolResult:
    from .stickers import extract_sticker_segments, get_sticker_store

    cfg = getattr(context, "config", None)
    if cfg is not None and not bool(getattr(cfg, "sticker_book_enable", True)):
        return ToolResult(
            tool_name="emoji", tool_call_id=call.id, success=False,
            error="表情本子关着", error_type="disabled",
        )

    session_id = str(getattr(context, "session_id", "") or "")
    store = get_sticker_store(cfg)
    args = call.arguments or {}
    op = str(args.get("op") or "list").lower()
    started = time.perf_counter()

    if op == "list":
        records = store.recent(session_id, limit=12)
        if not records:
            return ToolResult(
                tool_name="emoji", tool_call_id=call.id, success=True,
                output="本子还是空的。群里有人发表情就会自动收，也可以用 <<<EMOJI COLLECT 消息ID>>> 手动收。",
                data={"count": 0},
            )
        lines = [f"{index + 1}. {_describe(record)}" for index, record in enumerate(records)]
        return ToolResult(
            tool_name="emoji", tool_call_id=call.id, success=True,
            output="本子里最近这些（编号 1 是最新收的）：\n" + "\n".join(lines),
            data={"count": len(records)},
        )

    if op == "note":
        ref = str(args.get("ref") or "").strip()
        note = str(args.get("note") or "").strip()
        tags = [str(t) for t in (args.get("tags") or []) if str(t).strip()]
        if not ref or (not note and not tags):
            return ToolResult(
                tool_name="emoji", tool_call_id=call.id, success=False,
                error="用法：<<<EMOJI NOTE 编号 备注|#标签>>>", error_type="bad_args",
            )
        record = store.set_note(session_id, ref, note=note, tags=tags)
        if record is None:
            return ToolResult(
                tool_name="emoji", tool_call_id=call.id, success=False,
                error=f"本子里没有第 {ref} 张（先用 <<<EMOJI>>> 看看有什么）",
                error_type="not_found",
            )
        log_event("emoji.note", ref=ref, chars=len(note), tags=len(tags))
        return ToolResult(
            tool_name="emoji", tool_call_id=call.id, success=True,
            output=f"记下了：第 {ref} 张 = {_describe(record)}",
            data={"id": record.id},
        )

    if op == "collect":
        message_id = str(args.get("ref") or "").strip()
        bot = getattr(context, "bot", None)
        if not message_id.isdigit():
            return ToolResult(
                tool_name="emoji", tool_call_id=call.id, success=False,
                error="要消息ID数字", error_type="bad_args",
            )
        if bot is None:
            return ToolResult(
                tool_name="emoji", tool_call_id=call.id, success=False,
                error="缺少 bot 上下文", error_type="no_bot",
            )
        try:
            payload = await bot.get_msg(message_id=int(message_id))
            raw_message = payload.get("message") if isinstance(payload, dict) else None
            # get_msg 回来的是 [{type, data}, ...] 这种**字典**，而
            # extract_sticker_segments 只按 .type / .data 取值。这里包成
            # 轻量对象，不依赖各 OneBot 实现的消息构造签名（各家不一致）。
            message = [
                SimpleNamespace(type=item.get("type", ""), data=dict(item.get("data") or {}))
                for item in (raw_message or [])
                if isinstance(item, dict)
            ]
        except Exception as exc:  # noqa: BLE001 — 拿不到那条消息就明说
            return ToolResult(
                tool_name="emoji", tool_call_id=call.id, success=False,
                error=f"读不到那条消息（{type(exc).__name__}）", error_type="fetch_failed",
            )
        segments = extract_sticker_segments(message)
        if not segments:
            return ToolResult(
                tool_name="emoji", tool_call_id=call.id, success=True,
                output="那条消息里没有能收藏的表情（普通图片和系统表情不算）。",
                data={"added": 0},
            )
        added = await store.collect_segments(
            segments,
            session_id=session_id,
            context_text=str(getattr(context, "plain_text", "") or "")[:200],
            source_user=str(getattr(context, "user_id", "") or ""),
            bot=bot,
        )
        log_event("emoji.collect", message_id=message_id, added=added)
        return ToolResult(
            tool_name="emoji", tool_call_id=call.id, success=True,
            output=f"收进来了 {added} 张。" if added else "这几张本子里已经有了。",
            data={"added": added},
        )

    if op == "show":
        record = store.find(session_id, str(args.get("ref") or ""))
        if record is None:
            return ToolResult(
                tool_name="emoji", tool_call_id=call.id, success=False,
                error="本子里没有这一张（先用 <<<EMOJI>>> 看看有什么）",
                error_type="not_found",
            )
        return ToolResult(
            tool_name="emoji", tool_call_id=call.id, success=True,
            output=_describe(record),
            data={"value": record.to_segment()},
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    return ToolResult(
        tool_name="emoji", tool_call_id=call.id, success=False,
        error=f"不认识的操作 {op}", error_type="bad_op",
    )


def _visible_emoji(ctx: Any) -> bool:
    cfg = getattr(ctx, "config", None)
    if cfg is None:
        return False
    return bool(getattr(cfg, "sticker_book_enable", True))


def build_emoji_tool() -> ToolDefinition:
    return ToolDefinition(
        name="emoji",
        description="表情本子（翻列表 / 看图 / 写备注 / 手动收藏）",
        marker=EMOJI_MARKER,
        prompt_text=EMOJI_PROMPT_TEXT,
        parser=parse_emoji_request,
        executor=execute_emoji,
        failure_policy=FailurePolicy.FALLBACK_TEXT,
        # 翻本子必须回模型：她是拿列表里的编号去决定下一步动作的
        continuation=Continuation.RETURN_TO_MODEL,
        permission="social.emoji",
        visibility=_visible_emoji,
        owner="social_layer",
        # 排在 sticker(50) 之后：<<<STICKER ...>>> 的正则会吞掉 STICKER 开头的
        # 任何东西，先让表情发送解析完，再轮到本子
        priority=52,
        timeout=20.0,
        parameter_schema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["list", "show", "note", "collect"]},
                "ref": {"type": "string"},
                "note": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["op"],
        },
    )


def register_emoji_tool(registry: Any) -> None:
    registry.register(build_emoji_tool())


__all__ = [
    "EMOJI_MARKER",
    "EMOJI_PROMPT_TEXT",
    "build_emoji_tool",
    "execute_emoji",
    "parse_emoji_request",
    "register_emoji_tool",
]
