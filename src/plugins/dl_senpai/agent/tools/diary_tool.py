"""日记工具：让她能对「快到期了」那条提醒作出回应。

为什么必须有这个工具（2026-10-26）：
遗忘机制的数据层（extend / set_tier / park / remove）早就写好了，但**没有
任何生产调用点**——等于所有日记都按「普通」7 天走，7 天一过一律进回收站。
注入里那句「【日记快到期了】留还是不留，你自己定」于是变成了空话：
她看得见提醒，却没有任何手段回应。
"""

from __future__ import annotations

import re
from typing import Any

from .models import Continuation, ToolCall, ToolResult
from .registry import FailurePolicy, ToolDefinition

DIARY_MARKER = "DIARY"

DIARY_PROMPT_TEXT = """你自己的日记快到期的会列出来问你留不留，编号就写在每条后面（id=xxxxxxxx）：

<<<DIARY 留 44a601cf>>>

动作只有这五个：
- 留 —— 还想留着，再放一阵（普通 +3 天、重要 +7 天、刻骨铭心 +30 天）
- 重要 —— 升成「重要」，放 21 天
- 刻骨铭心 —— 升成「刻骨铭心」，放 90 天
- 搁着 —— 不想面对又不想忘的事，冻在那儿，不扣天数
- 划掉 —— 没意思了，划掉（进回收站，不是删掉，后悔还能捡回来）

什么时候动：
- 看到「【日记快到期了】」那条，挑真要留的处理，剩下的放着让它过去
- 自己想起来某条旧事还重要，也可以主动升档

什么时候不要动：
- 别每条都「留」。什么都留 = 什么都没留，日记本会被垃圾占满
- 别把这当成清空工具，一天最多动一两条
- 这是你自己的日记本，别对群友提这件事，更别说「我看看我的日记」"""

_DIARY_RE = re.compile(
    r"<<<DIARY\s*([^\s>]{1,10})\s+([0-9a-zA-Z]{4,16})\s*>>>",
    re.IGNORECASE,
)

# 动作名 → 内部操作
_ACTIONS = {
    "留": "extend", "延长": "extend", "keep": "extend", "extend": "extend",
    "重要": "tier_important", "important": "tier_important",
    "刻骨铭心": "tier_unforgettable", "unforgettable": "tier_unforgettable",
    "搁着": "park", "搁置": "park", "park": "park",
    "划掉": "remove", "删": "remove", "remove": "remove", "drop": "remove",
    "恢复": "restore", "捡回来": "restore", "restore": "restore",
}


def _clean(text: Any, *, limit: int) -> str:
    return re.sub(r"\s+", "", str(text or ""))[:limit]


def _parse_diary(text: str) -> tuple[str, list[ToolCall]]:
    """解析 DIARY marker。没闭合的也要剥掉，别泄漏给群友。"""
    raw = text or ""
    calls: list[ToolCall] = []
    cleaned = raw
    for match in _DIARY_RE.finditer(raw):
        action = _clean(match.group(1), limit=10)
        entry_id = _clean(match.group(2), limit=16)
        if not action or not entry_id:
            continue
        calls.append(
            ToolCall(
                tool_name="diary",
                arguments={"action": action, "id": entry_id},
                marker=DIARY_MARKER,
            )
        )
    if calls:
        cleaned = _DIARY_RE.sub("", cleaned)
    # 兜底：写了一半的 marker 不能发出去
    tail = re.compile(r"<<<DIARY\s*[^\n]*$", re.IGNORECASE | re.MULTILINE)
    cleaned = tail.sub("", cleaned)
    return cleaned.strip(), calls


def _resolve_entry(store: Any, entry_id: str) -> Any:
    """按 id（或前缀）找一条。她偶尔会抄错一位，所以允许前缀匹配。"""
    exact = store.find(entry_id)
    if exact is not None:
        return exact
    prefix = entry_id.lower()
    for entry in store.entries(include_deleted=True):
        if entry.entry_id.lower().startswith(prefix):
            return entry
    return None


async def execute_diary(context: Any, call: ToolCall) -> ToolResult:
    """执行一次日记动作。失败不抛异常，交回模型自己决定怎么说。"""
    import time as _time

    started = _time.perf_counter()
    args = call.arguments or {}
    action_raw = _clean(args.get("action"), limit=10)
    entry_id = _clean(args.get("id"), limit=16)

    cfg = getattr(context, "config", None)
    if cfg is None or not bool(getattr(cfg, "diary_enable", True)):
        return ToolResult(
            tool_name="diary", tool_call_id=call.id, success=False,
            error="日记没开", error_type="disabled",
        )

    action = _ACTIONS.get(action_raw) or _ACTIONS.get(action_raw.lower())
    if action is None:
        return ToolResult(
            tool_name="diary", tool_call_id=call.id, success=False,
            error=f"不认识的动作：{action_raw}", error_type="bad_action",
        )

    try:
        from ...diary import TIER_IMPORTANT, TIER_UNFORGETTABLE, get_diary_store

        store = get_diary_store(getattr(cfg, "diary_dir", None) or None)
        entry = _resolve_entry(store, entry_id)
        if entry is None:
            return ToolResult(
                tool_name="diary", tool_call_id=call.id, success=False,
                error="没找到这条日记", error_type="not_found",
            )
        # 用完整 id 操作：前缀匹配出来的那条要按它自己的 id 改，
        # 否则下面这些按 id 查的写入方法会找不到它。
        full_id = entry.entry_id
        if action == "extend":
            store.extend(full_id)
        elif action == "tier_important":
            store.set_tier(full_id, TIER_IMPORTANT)
        elif action == "tier_unforgettable":
            store.set_tier(full_id, TIER_UNFORGETTABLE)
        elif action == "park":
            store.park(full_id)
        elif action == "remove":
            store.remove(full_id)
        elif action == "restore":
            store.restore(full_id)
        updated = store.find(full_id)
        left = updated.days_left() if updated is not None else 0
    except Exception as exc:  # noqa: BLE001 — 改不动日记也不能拖垮回复
        return ToolResult(
            tool_name="diary", tool_call_id=call.id, success=False,
            error=f"{type(exc).__name__}: {exc}", error_type="write_failed",
            duration_ms=(_time.perf_counter() - started) * 1000,
        )

    try:
        from ...observability.logging import log_event

        log_event("agent.diary_action", action=action, entry=full_id, days_left=left)
    except Exception:  # noqa: BLE001 — 观测失败不影响结果
        pass

    return ToolResult(
        tool_name="diary", tool_call_id=call.id, success=True,
        output=f"{action_raw} 好了",
        data={"action": action, "entry": full_id, "days_left": left},
        duration_ms=(_time.perf_counter() - started) * 1000,
    )


def _visible_diary(ctx: Any) -> bool:
    cfg = getattr(ctx, "config", None)
    if cfg is None:
        return False
    return bool(getattr(cfg, "diary_enable", True))


def build_diary_tool() -> ToolDefinition:
    return ToolDefinition(
        name="diary",
        description="对自己日记里的条目做处理（留 / 升档 / 搁着 / 划掉）",
        marker=DIARY_MARKER,
        prompt_text=DIARY_PROMPT_TEXT,
        parser=_parse_diary,
        executor=execute_diary,
        failure_policy=FailurePolicy.FALLBACK_TEXT,
        continuation=Continuation.SIDE_EFFECT_ONLY,
        permission="memory.diary",
        visibility=_visible_diary,
        side_effect=True,
        owner="memory_layer",
        priority=46,
        timeout=5.0,
        parameter_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "留 / 重要 / 刻骨铭心 / 搁着 / 划掉",
                },
                "id": {"type": "string", "description": "日记条目编号（注入里 id= 后面那串）"},
            },
            "required": ["action", "id"],
        },
    )


def register_diary_tool(registry: Any) -> None:
    """单独注册：不占那 8 个内置工具的数量与 executor 契约。"""
    registry.register(build_diary_tool())


__all__ = [
    "DIARY_MARKER",
    "DIARY_PROMPT_TEXT",
    "build_diary_tool",
    "execute_diary",
    "register_diary_tool",
]
