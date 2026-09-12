"""群禁言：学姐被惹急时可短时禁言（伪指令 <<<MUTE>>>）。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

from .group_card import CardCandidate, CardCooldown, loads_marker_json

MuteKind = Literal["angry", "request"]

_MUTE_MARKER_RE = re.compile(r"<<<MUTE\s*(\{.*?\})\s*>>>", re.DOTALL | re.IGNORECASE)
_PROVOKE_RE = re.compile(
    r"(废物|垃圾|白痴|脑残|傻逼|傻b|sb|SB|nmsl|滚|去死|去死吧|操你|草你|"
    r"妈的|贱人|蠢猪|闭嘴啊?|你谁啊|打你|弄死|弱智|智障|狗东西|"
    r"臭猫娘|傻学姐|去死啦|有病|恶心|烦死)",
    re.IGNORECASE,
)
_MUTE_REQUEST_RE = re.compile(
    r"(禁言|口球|让.{0,4}闭嘴|安静一会|禁一下|禁我|把我禁|给我禁)"
)
_SELF_MUTE_RE = re.compile(r"(禁言我|禁我|把我禁言|把我禁|给我禁言|给我禁)")


@dataclass(frozen=True)
class MuteAction:
    user_id: int
    duration: int  # seconds
    kind: MuteKind


@dataclass
class MuteParseResult:
    text: str
    actions: list[MuteAction]


class MuteCooldown(CardCooldown):
    """禁言冷却（复用同一套 ready/mark）。"""


def looks_like_provocation(text: str) -> bool:
    return bool(_PROVOKE_RE.search(text or ""))


def looks_like_mute_request(text: str) -> bool:
    return bool(_MUTE_REQUEST_RE.search(text or ""))


def can_emit_mute(user_text: str) -> bool:
    """仅在被挑衅或明确要求禁言时允许执行 MUTE。"""
    return looks_like_provocation(user_text) or looks_like_mute_request(user_text)


def resolve_explicit_mute(
    chat_text: str,
    *,
    sender_id: int,
    at_ids: list[int],
    candidates: list[CardCandidate],
    duration: int = 60,
) -> tuple[MuteAction | None, str]:
    """解析「禁言我 / @他禁言」等明确指令。

    Returns:
        (action, block_reason) — action 可执行；block_reason 为 admin/not_found/""。
    """
    raw = (chat_text or "").strip()
    if not looks_like_mute_request(raw):
        return None, ""

    by_id = {c.user_id: c for c in candidates}
    target: int | None = None
    if _SELF_MUTE_RE.search(raw):
        target = int(sender_id)
    elif at_ids:
        target = int(at_ids[0])
    else:
        return None, ""

    cand = by_id.get(target)
    if cand is None:
        return None, "not_found"
    if cand.role != "member":
        return None, "admin"

    dur = max(10, int(duration))
    return MuteAction(user_id=target, duration=dur, kind="request"), ""


def mute_block_note(reason: str) -> str:
    if reason == "admin":
        return "（你是管理/群主啊，学姐禁不了你啦～换个普通成员身份才能被禁。）"
    if reason == "not_found":
        return "（要禁谁？@一下对方，或者自己说「禁言我」。）"
    if reason == "cooldown":
        return "（禁言还在冷却，过两分钟再作死哦～）"
    if reason == "failed":
        return "（禁言没成功，可能是权限不够或接口抽风了。）"
    if reason == "no_marker":
        return "（禁言没真正执行，你再说一次「禁言我」学姐再试。）"
    return ""


def parse_mute_actions(raw_reply: str) -> MuteParseResult:
    actions: list[MuteAction] = []
    for match in _MUTE_MARKER_RE.finditer(raw_reply or ""):
        data = loads_marker_json(match.group(1))
        if not data:
            continue
        uid_raw = data.get("user_id", data.get("userId"))
        try:
            user_id = int(uid_raw)
        except (TypeError, ValueError):
            continue
        try:
            duration = int(data.get("duration") or data.get("seconds") or 60)
        except (TypeError, ValueError):
            duration = 60
        kind_raw = str(data.get("kind") or "angry").lower()
        kind: MuteKind = "request" if kind_raw == "request" else "angry"
        actions.append(MuteAction(user_id=user_id, duration=duration, kind=kind))

    cleaned = _MUTE_MARKER_RE.sub("", raw_reply or "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return MuteParseResult(text=cleaned, actions=actions)


def filter_mute_actions(
    actions: list[MuteAction],
    *,
    candidates: list[CardCandidate],
    max_duration: int,
    min_duration: int,
    ready: bool,
) -> tuple[list[MuteAction], str]:
    """返回 (通过的动作, block_reason)。"""
    if not ready:
        return [], "cooldown"
    by_id = {c.user_id: c for c in candidates}
    out: list[MuteAction] = []
    seen: set[int] = set()
    max_duration = max(10, int(max_duration))
    min_duration = max(10, min(int(min_duration), max_duration))
    saw_admin = False
    saw_missing = False
    for action in actions:
        cand = by_id.get(action.user_id)
        if cand is None:
            saw_missing = True
            continue
        if cand.role != "member":
            saw_admin = True
            continue
        if action.user_id in seen:
            continue
        duration = max(min_duration, min(max_duration, int(action.duration)))
        seen.add(action.user_id)
        out.append(
            MuteAction(user_id=action.user_id, duration=duration, kind=action.kind)
        )
        break
    if out:
        return out, ""
    if saw_admin:
        return [], "admin"
    if saw_missing:
        return [], "not_found"
    return [], ""


async def apply_mute_actions(
    bot: Bot,
    *,
    group_id: int,
    actions: list[MuteAction],
) -> list[MuteAction]:
    applied: list[MuteAction] = []
    for action in actions:
        try:
            await bot.set_group_ban(
                group_id=group_id,
                user_id=action.user_id,
                duration=action.duration,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"dl_senpai set_group_ban failed group={group_id} user={action.user_id}"
            )
            continue
        applied.append(action)
        logger.info(
            f"dl_senpai set_group_ban group={group_id} "
            f"user={action.user_id} duration={action.duration}s kind={action.kind}"
        )
    return applied


def mute_summary_for_memory(actions: list[MuteAction]) -> str:
    # 不写进记忆正文，避免模型模仿成伪日志泄漏
    return ""


def format_mute_ack(actions: list[MuteAction]) -> str:
    if not actions:
        return ""
    a = actions[0]
    mins = max(1, round(a.duration / 60)) if a.duration >= 60 else 0
    if mins:
        return f"（哼，先让你安静 {mins} 分钟！）"
    return f"（哼，先让你安静 {a.duration} 秒！）"
