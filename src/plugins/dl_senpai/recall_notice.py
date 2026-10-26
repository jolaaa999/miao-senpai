"""撤回通知：别人的撤回只标记流水，她自己的被撤回才按冷却说一句。

为什么分两种处理：
- 别人的消息被撤回 → 她记忆里那条已经不作数了，必须标掉，否则会拿被撤回的内容当事实；
- 她自己的消息被撤回 → 这是"针对我"的事件，允许她当场接一句（灵灵那种反应），
  但要限流：撤回通知密集、且群里天天有人撤，不冷却就会变成追着每条撤回喊。
"""

from __future__ import annotations

import re
import time
from typing import Any

from nonebot import logger

from .config import get_config
from .gateway.context import NOTICE_FRIEND_RECALL, NOTICE_GROUP_RECALL
from .recent_chat import get_recent_chat_store
from .style import polish_reply

# 冷却按会话记：同一个群里连续撤回她三条，也只说一句。
_LAST_REPLY_AT: dict[str, float] = {}

_MAX_CHARS = 60

_SYSTEM = (
    "你在 QQ 群里聊天，人设是热情可爱的深度学习学姐，说话短、口语、不用 Markdown。"
    "现在发生了一件事：你刚发出去的一条消息被撤回了。"
    "用一句很短的中文反应一下（30 字以内）：可以疑惑、假装委屈、自嘲，别复述原文，"
    "别解释技术细节，别写「我应该…」这类内心分析，也不要输出任何指令标记。"
)


def cooldown_left(session_id: str, cooldown: float, *, now: float | None = None) -> float:
    """距离下次允许开口还剩多少秒（0 表示可以）。"""
    moment = time.time() if now is None else float(now)
    if not session_id or cooldown <= 0:
        return 0.0
    last = _LAST_REPLY_AT.get(session_id, 0.0)
    return max(0.0, cooldown - (moment - last))


def reset_cooldown() -> None:
    """清空冷却表（测试与热重载用）。"""
    _LAST_REPLY_AT.clear()


async def _compose(cfg: Any, *, operator_is_self: bool) -> str:
    """生成一句反应；任何失败都返回空串（宁可不说话，也不要发出半成品）。"""
    from .llm import get_llm

    who = "是我自己撤回的" if operator_is_self else "被别人（可能是管理员）撤回"
    llm = get_llm(cfg)
    resp = await llm.client.chat.completions.create(
        model=cfg.active_model(),
        messages=[
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": f"（系统事件：你刚发的一条消息{who}。）"},
        ],
        temperature=0.8,
        max_tokens=200,
        timeout=20,
    )
    raw = (resp.choices[0].message.content or "").strip()
    if not raw:
        return ""
    # 过一遍正式的风格清洗：这里最容易漏出「我已经发了…」这类旁注与残留标记。
    text = polish_reply(raw, speaker="学姐", session_key="recall", allow_fallback=False)
    text = re.sub(r"\s{2,}", " ", text or "").strip()
    if len(text) > _MAX_CHARS:
        text = text[:_MAX_CHARS].rstrip()
    return text


async def handle_recall(ctx: Any, *, config: Any = None) -> str:
    """处理一条撤回通知，返回做了什么（给日志与单测断言用）。"""
    cfg = config or get_config()
    notice_type = str(getattr(ctx, "notice_type", "") or "")
    if notice_type not in (NOTICE_GROUP_RECALL, NOTICE_FRIEND_RECALL):
        return "ignored"

    self_id = str(getattr(ctx, "self_id", "") or "")
    author = str(getattr(ctx, "recall_author_id", "") or "")
    operator = str(getattr(ctx, "recall_operator_id", "") or "") or author
    message_id = str(getattr(ctx, "recalled_message_id", "") or "")
    session_id = str(getattr(ctx, "session_id", "") or "")

    # 别人的消息被撤回：标掉流水，让她别再引用那句已经不存在的话。
    if author != self_id:
        try:
            marked = get_recent_chat_store().mark_recalled(session_id, message_id)
        except Exception as exc:  # noqa: BLE001 — 标记失败不该影响后续消息
            logger.debug(f"dl_senpai recall: mark failed err={exc}")
            marked = False
        return "other_marked" if marked else "other_seen"

    # 以下都是「她自己的消息被撤回」
    if not bool(getattr(cfg, "recall_reply_enable", True)):
        return "own_silent"
    if notice_type == NOTICE_FRIEND_RECALL:
        # 私聊里她被撤回也不接话：没有群语境、又容易变成打扰。
        return "own_private_skipped"
    cooldown = float(getattr(cfg, "recall_reply_cooldown", 180) or 0)
    if cooldown_left(session_id, cooldown) > 0:
        return "own_cooldown"

    bot = getattr(ctx, "bot", None)
    group_id = str(getattr(ctx, "group_id", "") or "")
    if bot is None or not group_id:
        return "own_no_target"
    if bool(getattr(cfg, "dry_run", False)):
        logger.info("[干跑] 自己的消息被撤回，她会说一句（不真发）")
        return "own_dry_run"

    try:
        text = await _compose(cfg, operator_is_self=operator == author)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai recall: compose failed err={type(exc).__name__}: {exc}")
        return "own_compose_failed"
    if not text:
        return "own_empty"

    # 主动发言必须过熔断/限流：这条路径没人邀请她说，最容易踩风控。
    from .send_guard import get_send_guard

    guard = get_send_guard()
    allowed, reason = guard.before_send()
    if not allowed:
        logger.info(f"dl_senpai recall reply blocked ({reason})")
        return "own_blocked"
    try:
        await bot.send_group_msg(group_id=int(group_id), message=text)
    except Exception as exc:  # noqa: BLE001
        guard.on_failure()
        logger.warning(f"dl_senpai recall send failed group={group_id}: {exc}")
        return "own_send_failed"
    guard.on_success()
    _LAST_REPLY_AT[session_id] = time.time()
    logger.info(
        f"dl_senpai recall replied group={group_id} chars={len(text)} "
        f"operator_self={operator == author}"
    )
    return "own_replied"


__all__ = ["handle_recall", "cooldown_left", "reset_cooldown"]
