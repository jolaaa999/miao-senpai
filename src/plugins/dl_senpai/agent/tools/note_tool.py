"""记心事工具：让她自己把「硌在心里的小事」写下来。

为什么需要这个工具（2026-10-25）：
inner_state 里已经有心绪手札的数据结构和注入，但**没人往里写**——
手札永远空着，心情分层就是死的。这个工具把手札的写入权交给模型自己：
她什么时候觉得有件事没过去，就记一笔。

三条设计约束，都是为了不让它变成新的 AI 味来源：
1. **不强制**：闲聊、玩笑、已经被安慰过的事都不用记。手札是「不配写进日记
   但确实硌着」的那一小撮，不是每轮都要交的作业。
2. **不点数**：记下来只是记下来。要不要因此闹脾气她自己决定——系统不点火，
   否则会出现「无缘无故发脾气、你永远不知道火从哪来」。
3. **不念出来**：写了就是写在她的私处，下一轮注入时她自己能看到，
   但绝不能对群友说「我记下了」「我写到心事本上了」。
"""

from __future__ import annotations

import re
from typing import Any

from .models import Continuation, ToolCall, ToolResult
from .registry import FailurePolicy, ToolDefinition

NOTE_MARKER = "NOTE"

# 一条心事最多这么多字：超过就不是「一句话」，是日记
_NOTE_TEXT_MAX = 40
_NOTE_TAG_MAX = 8

NOTE_PROMPT_TEXT = """心里有件小事硌得慌、但又不至于写进日记，可以记一笔：

<<<NOTE 标签|一句话>>>

什么时候记（很少，一天最多两三条）：
- 被晾着、被冷落，问了一句没人接
- 被夸到、被在意了，心里动了一下
- 有点不安、吃味、替对方难受，但没打算说出来
- 有人说了句戳到你的话，你嘴上没接

什么时候不要记：
- 正常聊天、玩笑、接梗——不要每轮都记
- 对方明确说了难受、你已经认真安慰过的事
- 已经写进日记的大事

怎么记：
- 标签两到四个字，写情绪类别：被冷落 / 被夸 / 不安 / 吃醋 / 有点闷
- 竖线后面一句话，四十字以内，写当时的感觉，不是复述对方的话
- 一次一条；同样的心事连着发生才再记一条

铁律：
- 这些是你自己的心事，**对群友一个字都别提**，别说「我记下来了」
- 不要用这个工具刷存在感，也不要用它给自己加戏
- 记了不代表要发作。要不要变冷淡，你自己看着办"""

# <<<NOTE 标签|正文>>>。标签可省；正文里出现竖线也算正文的一部分。
_NOTE_RE = re.compile(r"<<<NOTE\s*([^|｜>]{0,12})[|｜]([^>]{1,400})>>>", re.IGNORECASE)
# 只写了正文没给标签的写法也要收
_NOTE_RE_LOOSE = re.compile(r"<<<NOTE\s*([^|｜>]{1,400})>>>", re.IGNORECASE)


def _clean(text: Any, *, limit: int) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()[:limit]


def _parse_note(text: str) -> tuple[str, list[ToolCall]]:
    """解析 NOTE marker。没闭合的也要剥掉，别泄漏给群友。"""
    raw = text or ""
    calls: list[ToolCall] = []
    cleaned = raw

    for match in _NOTE_RE.finditer(raw):
        tag = _clean(match.group(1), limit=_NOTE_TAG_MAX)
        content = _clean(match.group(2), limit=_NOTE_TEXT_MAX)
        if not content:
            continue
        calls.append(
            ToolCall(
                tool_name="note",
                arguments={"tag": tag, "text": content},
                marker=NOTE_MARKER,
            )
        )
    if calls:
        cleaned = _NOTE_RE.sub("", cleaned)
    else:
        for match in _NOTE_RE_LOOSE.finditer(raw):
            content = _clean(match.group(1), limit=_NOTE_TEXT_MAX)
            if not content:
                continue
            calls.append(
                ToolCall(
                    tool_name="note",
                    arguments={"tag": "", "text": content},
                    marker=NOTE_MARKER,
                )
            )
        if calls:
            cleaned = _NOTE_RE_LOOSE.sub("", cleaned)

    # 兜底：写了一半的 marker 不能发出去
    tail = re.compile(r"<<<NOTE\s*[^\n]*$", re.IGNORECASE | re.MULTILINE)
    cleaned = tail.sub("", cleaned)
    return cleaned.strip(), calls


def _note_scope_key(context: Any, cfg: Any) -> str:
    """心事写在哪个作用域：跟内心状态用同一把钥匙，否则写了也读不到。

    走 facade 的纯函数，不 import 存储：agent/ 层直接碰 inner_state 的
    存储会被架构守卫测试判违规（tests/test_phase55_boundaries.py）。
    """
    from ..memory_facade import MemoryFacade

    return MemoryFacade.inner_scope_key(
        per_group=bool(getattr(cfg, "inner_state_per_group", True)),
        group_id=(getattr(context, "group_id", "") or None),
        private=bool(getattr(context, "private_chat", False)),
    )


async def execute_note(context: Any, call: ToolCall) -> ToolResult:
    """写一条心绪手札。失败不抛异常，交回模型自己决定怎么说。"""
    import time

    started = time.perf_counter()
    args = call.arguments or {}
    content = _clean(args.get("text"), limit=_NOTE_TEXT_MAX)
    tag = _clean(args.get("tag"), limit=_NOTE_TAG_MAX)
    if not content:
        return ToolResult(
            tool_name="note", tool_call_id=call.id, success=False,
            error="心事是空的", error_type="empty_note",
        )

    cfg = getattr(context, "config", None)
    if cfg is None or not bool(getattr(cfg, "inner_state_enable", True)):
        return ToolResult(
            tool_name="note", tool_call_id=call.id, success=False,
            error="心事本没开", error_type="disabled",
        )

    try:
        from ..memory_facade import MemoryFacade

        key = _note_scope_key(context, cfg)
        MemoryFacade(config=cfg).add_inner_note(key, content, tag)
    except Exception as exc:  # noqa: BLE001 — 记不下来也不能拖垮回复
        return ToolResult(
            tool_name="note", tool_call_id=call.id, success=False,
            error=f"{type(exc).__name__}: {exc}", error_type="write_failed",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    try:
        from ...observability.logging import log_event

        log_event("agent.note", tag=tag or None, chars=len(content))
    except Exception:  # noqa: BLE001 — 观测失败不影响结果
        pass

    # 输出刻意很短：这条不会回到模型（continuation=SIDE_EFFECT_ONLY），
    # 只用于日志排查，不能写成一段会诱导模型复述的话。
    return ToolResult(
        tool_name="note", tool_call_id=call.id, success=True,
        output="已记下",
        data={"tag": tag, "chars": len(content)},
        duration_ms=(time.perf_counter() - started) * 1000,
    )


def _visible_note(ctx: Any) -> bool:
    """内心状态没开就别暴露这个工具，否则她会去调一个必然失败的写入口。"""
    cfg = getattr(ctx, "config", None)
    if cfg is None:
        return False
    return bool(getattr(cfg, "inner_state_enable", True))


def build_note_tool() -> ToolDefinition:
    return ToolDefinition(
        name="note",
        description="记一条心绪手札（不配写进日记、但硌在心里的小事）",
        marker=NOTE_MARKER,
        prompt_text=NOTE_PROMPT_TEXT,
        parser=_parse_note,
        executor=execute_note,
        failure_policy=FailurePolicy.FALLBACK_TEXT,
        # 写盘成功就够，不需要再给模型一轮：省一次整段上下文重发
        continuation=Continuation.SIDE_EFFECT_ONLY,
        permission="memory.note",
        visibility=_visible_note,
        side_effect=True,
        owner="memory_layer",
        priority=45,
        timeout=5.0,
        parameter_schema={
            "type": "object",
            "properties": {
                "tag": {
                    "type": "string",
                    "description": "情绪类别，两到四个字：被冷落 / 被夸 / 不安 / 吃醋",
                },
                "text": {
                    "type": "string",
                    "description": "一句话，四十字以内，写当时的感觉",
                },
            },
            "required": ["text"],
        },
    )


def register_note_tool(registry: Any) -> None:
    """单独注册：内置那 8 个工具的数量与 executor 身份被守卫测试钉着。"""
    registry.register(build_note_tool())


__all__ = [
    "NOTE_MARKER",
    "NOTE_PROMPT_TEXT",
    "build_note_tool",
    "execute_note",
    "register_note_tool",
]
