"""反馈记录：她对自己哪句话说得不像人，记一笔。

为什么单独一个模块（2026-10-27）：

qq-bridge 有 qq_report_feedback，用途是「这次输出有问题就记下来，别只是
在对话里道歉了事」。学姐这边没有任何闭环——她说了一句很 AI 的话、答不上
来、被群友直接吐槽，这些信号全都散落在日志里，没人看得见，也就无从调整
提示词和阈值。

三条设计：

1. **记给自己看，不给人看**：写下就完了。绝不能在对话里说「我记一下反馈」
   ——那比犯的错更机器味。
2. **只写不判**：这里不评分、不触发任何自动调参。阈值和提示词是人改的，
   让程序根据「她自评差」自动收紧，最后只会收紧成客服。
3. **落 jsonl 且轮转**：追加写，超过上限就滚成 .1 备份，防止无限长。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .agent.tools.models import Continuation, ToolCall, ToolResult
from .agent.tools.registry import FailurePolicy, ToolDefinition
from .observability.logging import log_event

FEEDBACK_MARKER = "FEEDBACK"

# 类别做白名单：自由文本会变成「吐槽合集」，分类之后才看得出趋势
_KINDS = ("不像人", "没答上来", "被冷落", "群友不满", "被夸")
_TEXT_MAX = 80
_MAX_LINES = 2000

FEEDBACK_PROMPT_TEXT = """【反馈】
偶尔会有一句话说出来自己就知道不对——太像客服、太像百科、答非所问、
或者在不懂装懂。这种时候可以记一笔：

<<<FEEDBACK 类别|一句话>>>

类别只能从这几个里选：不像人 / 没答上来 / 被冷落 / 群友不满 / 被夸

什么时候记：你回完之后自己觉得别扭、对方明显不满意、你发现自己在硬撑。
什么时候别记：正常的闲聊、玩笑、接梗——记多了这条记录就没意义了。
也不用每次都记，一天最多一两条。

**记完什么都不要说**。不要讲「我记下了」「我下次注意」，也不要道歉补充。
它只是写在你自己的本子上。
"""


def feedback_path(config: Any = None) -> Path:
    raw = getattr(config, "feedback_dir", "") or "data/dl_senpai/feedback"
    return Path(str(raw)) / "feedback.jsonl"


def _rotate(path: Path, max_lines: int) -> None:
    """行数超上限就滚一份备份。

    为什么按行数而不是大小：这些记录是给人看的，行数直接对应「有多少条」，
    比字节数好判断。
    """
    try:
        if not path.exists():
            return
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            count = sum(1 for _ in fh)
        if count < max_lines:
            return
        backup = path.with_suffix(".jsonl.1")
        if backup.exists():
            backup.unlink()
        path.rename(backup)
    except OSError as exc:
        log_event("feedback.rotate_failed", error=f"{type(exc).__name__}: {exc}"[:160])


def append_feedback(
    kind: str,
    text: str,
    *,
    config: Any = None,
    session_id: str = "",
    user_id: str = "",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """追加一条反馈，返回落盘的那条记录（便于测试断言）。"""
    label = str(kind or "").strip()
    if label not in _KINDS:
        label = "不像人"  # 认不出来的类别按最常见的那类记，不丢数据
    body = str(text or "").strip()[:_TEXT_MAX]
    record = {
        "ts": round(time.time(), 3),
        "kind": label,
        "text": body,
        "session": session_id,
        "user": user_id,
    }
    if extra:
        record["extra"] = extra
    path = feedback_path(config)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path, _MAX_LINES)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        log_event("feedback.write_failed", error=f"{type(exc).__name__}: {exc}"[:160])
    return record


def read_feedback(config: Any = None, *, limit: int = 20) -> list[dict[str, Any]]:
    """读最近若干条（只给主人看，不进提示词）。"""
    path = feedback_path(config)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    for line in lines[-max(1, limit):]:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def parse_feedback_request(raw_reply: str) -> tuple[str, list[ToolCall]]:
    import re

    pattern = re.compile(r"<<<\s*FEEDBACK\s*([^>]*)>>>", re.IGNORECASE)
    text = raw_reply or ""
    calls: list[ToolCall] = []
    for match in pattern.finditer(text):
        body = (match.group(1) or "").strip()
        kind, _, detail = body.partition("|")
        calls.append(
            ToolCall(
                tool_name="feedback",
                arguments={"kind": kind.strip(), "text": detail.strip()},
                raw=match.group(0),
                marker=FEEDBACK_MARKER,
            )
        )
    return pattern.sub("", text), calls


async def execute_feedback(context: Any, call: ToolCall) -> ToolResult:
    cfg = getattr(context, "config", None)
    if cfg is not None and not bool(getattr(cfg, "feedback_enable", True)):
        return ToolResult(
            tool_name="feedback", tool_call_id=call.id, success=False,
            error="反馈记录关着", error_type="disabled",
        )
    args = call.arguments or {}
    record = append_feedback(
        str(args.get("kind") or ""),
        str(args.get("text") or ""),
        config=cfg,
        session_id=str(getattr(context, "session_id", "") or ""),
        user_id=str(getattr(context, "user_id", "") or ""),
    )
    if not record.get("text"):
        return ToolResult(
            tool_name="feedback", tool_call_id=call.id, success=False,
            error="没写内容", error_type="empty_text",
        )
    log_event("agent.feedback", kind=record["kind"], chars=len(record["text"]))
    return ToolResult(
        tool_name="feedback", tool_call_id=call.id, success=True,
        output="记下了",
        data={"kind": record["kind"]},
    )


def _visible_feedback(ctx: Any) -> bool:
    cfg = getattr(ctx, "config", None)
    if cfg is None:
        return False
    return bool(getattr(cfg, "feedback_enable", True))


def build_feedback_tool() -> ToolDefinition:
    return ToolDefinition(
        name="feedback",
        description="记一条自评反馈（哪句话不像人、哪次没答上来）",
        marker=FEEDBACK_MARKER,
        prompt_text=FEEDBACK_PROMPT_TEXT,
        parser=parse_feedback_request,
        executor=execute_feedback,
        failure_policy=FailurePolicy.FALLBACK_TEXT,
        continuation=Continuation.SIDE_EFFECT_ONLY,
        permission="memory.feedback",
        visibility=_visible_feedback,
        side_effect=True,
        owner="social_layer",
        priority=47,
        timeout=5.0,
        parameter_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": list(_KINDS)},
                "text": {"type": "string"},
            },
            "required": ["kind", "text"],
        },
    )


def register_feedback_tool(registry: Any) -> None:
    registry.register(build_feedback_tool())


__all__ = [
    "FEEDBACK_MARKER",
    "FEEDBACK_PROMPT_TEXT",
    "append_feedback",
    "build_feedback_tool",
    "execute_feedback",
    "feedback_path",
    "parse_feedback_request",
    "read_feedback",
    "register_feedback_tool",
]
