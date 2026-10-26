"""断线补课：重连之后，把断线期间错过的消息补上。

为什么需要它
------------
原版进阶篇第六节写得很直白：桥接和协议端之间那条长连接断了以后，
**断线期间的消息是真的丢了，不会补发**。她的表现就是「还活着，但不理我」，
或者更糟：第二天开机像失忆，因为她的上下文里根本没有昨天那段。

补课的三条纪律（第三条最重要）
------------------------------
1. **只补一次**：按「上次检查到哪儿」增量拉，不重拉整段历史。
2. **有时间窗和条数上限**：关机一周末再开机，不该把三百条灌进上下文。
3. **只总结、不逐条回**。原版第一版就是「拉回来就无脑回一遍」，
   结果开机后她一口气回了 30 条，群里看着像抽风。

实现上，补课结果**不直接进聊天**，而是写成一份待注入的摘要
（backfill/pending.json），由 context_extras 在下一次拼提示词时
注入一次然后删掉——这样「补课」永远只是背景知识，不会变成抢答。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from nonebot import logger

from .storage import JsonStore

__all__ = [
    "backfill_dir",
    "collect_missed",
    "pending_path",
    "run_backfill",
    "take_pending_brief",
]

# 单条消息摘到这么长就截断：补课要的是「聊了什么」，不是原文全文
_MAX_LINE_CHARS = 80


def backfill_dir(cfg: Any) -> Path:
    raw = getattr(cfg, "backfill_dir", "") or ""
    if raw:
        return Path(str(raw))
    base = Path(str(getattr(cfg, "memory_dir", "data/dl_senpai/memory")))
    return base.parent / "backfill"


def _state_path(cfg: Any) -> Path:
    return backfill_dir(cfg) / "state.json"


def pending_path(cfg: Any) -> Path:
    return backfill_dir(cfg) / "pending.json"


def _text_of(message: Any) -> str:
    """把 OneBot 消息段压成一行纯文本，非文本段用占位符。"""
    if isinstance(message, str):
        return message.strip()
    parts: list[str] = []
    for seg in message or []:
        if not isinstance(seg, dict):
            continue
        kind = str(seg.get("type") or "")
        data = seg.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "image":
            parts.append("[图片]")
        elif kind == "record":
            parts.append("[语音]")
        elif kind == "face":
            parts.append("[表情]")
        elif kind == "at":
            continue
    return "".join(parts).strip()


async def _call(bot: Any, action: str, **params: Any) -> Any:
    try:
        return await bot.call_api(action, **params)
    except Exception as exc:  # noqa: BLE001 — 协议端不支持这个接口是常态
        logger.info(f"dl_senpai backfill api {action} unsupported: {type(exc).__name__}: {exc}")
        return None


async def _recent_private_targets(bot: Any) -> list[str]:
    """猜最近私聊过的人。NapCat 有 get_recent_contact，其它实现可能没有。"""
    data = await _call(bot, "get_recent_contact", count=20)
    targets: list[str] = []
    if not isinstance(data, list):
        return targets
    for item in data:
        try:
            peer = str(item.get("peerUin") or item.get("user_id") or "").strip()
            chat_type = int(item.get("chatType") or 0)
        except Exception:  # noqa: BLE001
            continue
        # chatType 1 = 私聊（OneBot 侧习惯值）；只要私聊，群聊走另一条路
        if peer and chat_type in (0, 1):
            targets.append(peer)
    return targets[:10]


async def collect_missed(bot: Any, cfg: Any, *, now: float | None = None) -> list[dict[str, Any]]:
    """拉出「上次检查之后」的消息。返回 [{scope, name, text, time}]。"""
    now = now or time.time()
    state = JsonStore(_state_path(cfg), create_parent=True).load(default={})
    since = float(state.get("last_check_at") or 0.0)
    max_age = float(getattr(cfg, "backfill_max_age_sec", 21600) or 0)
    if since and max_age > 0:
        since = max(since, now - max_age)

    limit = int(getattr(cfg, "backfill_max_messages", 30) or 30)
    dropped: list[dict[str, Any]] = []
    groups = sorted(getattr(cfg, "allowed_group_ids", lambda: set())())

    for group_id in groups:
        data = await _call(
            bot, "get_group_msg_history", group_id=int(group_id), count=max(20, limit)
        )
        messages = (data or {}).get("messages") if isinstance(data, dict) else None
        for item in messages or []:
            dropped.append(_normalize(item, scope="group", scope_name=str(group_id)))

    for peer in await _recent_private_targets(bot):
        data = await _call(bot, "get_friend_msg_history", user_id=int(peer), count=max(20, limit))
        messages = (data or {}).get("messages") if isinstance(data, dict) else None
        for item in messages or []:
            dropped.append(_normalize(item, scope="private", scope_name=str(peer)))

    fresh = [
        item
        for item in dropped
        if item["time"] > since and item["time"] <= now and item["text"] and not item["self"]
    ]
    fresh.sort(key=lambda x: x["time"])
    return fresh[-limit:]


def _normalize(item: Any, *, scope: str, scope_name: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"scope": scope, "scope_name": scope_name, "name": "", "text": "", "time": 0.0, "self": False}
    sender = item.get("sender") or {}
    try:
        ts = float(item.get("time") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    text = _text_of(item.get("message") or item.get("raw_message") or "")
    name = str(sender.get("card") or sender.get("nickname") or sender.get("user_id") or "")
    return {
        "scope": scope,
        "scope_name": scope_name,
        "name": name[:20],
        "text": text[:_MAX_LINE_CHARS],
        "time": ts,
        "self": bool(item.get("self")),
    }


def _render(rows: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in rows:
        import time as _t

        stamp = _t.strftime("%m-%d %H:%M", _t.localtime(row["time"])) if row["time"] else "--"
        where = "群" if row["scope"] == "group" else "私聊"
        lines.append(f"{stamp} {where}{row['scope_name']} {row['name']}: {row['text']}")
    return "\n".join(lines)


async def run_backfill(bot: Any, cfg: Any) -> str:
    """重连后跑一次。返回结果标签，方便日志与单测断言。"""
    if not bool(getattr(cfg, "backfill_enable", False)):
        return "disabled"

    now = time.time()
    state_path = _state_path(cfg)
    state = JsonStore(state_path, create_parent=True).load(default={})
    if not float(state.get("last_check_at") or 0.0):
        # 首次启用只打基线。否则第一次打开开关就会把能拉到的历史全灌进上下文，
        # 表现是「她突然对昨天的事发表感想」——比不补更吓人。
        JsonStore(state_path, create_parent=True).save({"last_check_at": now})
        logger.info("dl_senpai backfill baselined (first run)")
        return "baselined"

    try:
        rows = await collect_missed(bot, cfg, now=now)
    except Exception as exc:  # noqa: BLE001 — 补课失败绝不能影响正常聊天
        logger.warning(f"dl_senpai backfill failed: {type(exc).__name__}: {exc}")
        JsonStore(state_path, create_parent=True).save({"last_check_at": now})
        return "failed"

    JsonStore(state_path, create_parent=True).save({"last_check_at": now})
    if not rows:
        return "empty"

    brief = _render(rows)
    JsonStore(pending_path(cfg), create_parent=True).save(
        {"text": brief, "count": len(rows), "at": now}
    )
    logger.info(f"dl_senpai backfill queued messages={len(rows)}")
    return f"queued({len(rows)})"


def take_pending_brief(cfg: Any) -> str:
    """取走待注入的补课摘要（取走即删，保证只注入一次）。"""
    path = pending_path(cfg)
    if not path.exists():
        return ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        data = {}
    try:
        path.unlink()
    except OSError:
        pass
    text = str((data or {}).get("text") or "").strip()
    count = int((data or {}).get("count") or 0)
    if not text:
        return ""
    return (
        "【断线补课】你刚才掉线了一会儿，这段时间群里/私聊里有人说过这些话"
        f"（共 {count} 条）。这只是背景，**不要逐条回应**，也不要假装你当时在场；"
        "如果需要，用一句话提一下「刚才没在」就够了：\n" + text
    )
