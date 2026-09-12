from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from typing import Literal

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, MessageEvent

CardKind = Literal["request", "playful"]

# 模型在正文后附带的隐式指令，发送前会剥掉
_CARD_MARKER_RE = re.compile(r"<<<CARD\s*(\{.*?\})\s*>>>", re.DOTALL | re.IGNORECASE)
# 明确否定：别再改 / 不要改 / 不准改 …
_CARD_NEGATE_RE = re.compile(
    r"(别|不要|别再|不要再|不准|禁止|别给我|停止|取消|补药).{0,8}"
    r"(改|换|设置|弄).{0,10}(群名片|群名称|群名|名片|昵称|外号|网名|备注)"
)
# 用户明显在要求改名片 / 起外号（含口语「群名称」=群名片）
_CARD_INTENT_RE = re.compile(
    r"(改|换|设置|整|搞|弄).{0,10}(群名片|群名称|群名|名片|昵称|外号|网名|备注)"
    r"|(群名片|群名称|群名|名片|昵称|外号|网名|备注).{0,6}(改|换|设置|成)"
    r"|把(他|她|它|其|这|那).{0,8}(群名片|群名称|群名|名片|外号|昵称).{0,4}(改|换|成)"
    r"|给我(改|换|起|取).{0,6}(名|叫|外号|名片|群名)"
    r"|叫我[\u4e00-\u9fffA-Za-z0-9]{1,12}"
    r"|把我(改|换|叫)成"
)


@dataclass(frozen=True)
class CardCandidate:
    user_id: int
    display_name: str
    current_card: str
    role: str  # owner / admin / member
    relation: str  # sender / at / self


@dataclass(frozen=True)
class CardAction:
    user_id: int
    card: str
    kind: CardKind


@dataclass
class CardParseResult:
    text: str
    actions: list[CardAction]


class CardCooldown:
    """主动改名片冷却（对方明确要求不走这里）。"""

    def __init__(self) -> None:
        self._last: dict[str, float] = {}

    def ready(self, session_id: str, cooldown_sec: int, now: float | None = None) -> bool:
        if cooldown_sec <= 0:
            return True
        if session_id not in self._last:
            return True
        now = time.time() if now is None else now
        return (now - self._last[session_id]) >= cooldown_sec

    def mark(self, session_id: str, now: float | None = None) -> None:
        self._last[session_id] = time.time() if now is None else now


def looks_like_card_request(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return False
    # 「补药/不要再改群名称了」这类应视为拒绝，不要触发改名片补救
    if _CARD_NEGATE_RE.search(raw):
        return False
    return _CARD_INTENT_RE.search(raw) is not None


def sanitize_card(raw: str, *, max_len: int) -> str | None:
    text = (raw or "").strip()
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    if not text:
        return None
    # 简单拦截明显不当内容
    banned = ("http://", "https://", "www.", "加群", "出售", "色情", "裸体")
    lower = text.lower()
    if any(b in lower or b in text for b in banned):
        return None
    if max_len > 0 and len(text) > max_len:
        text = text[:max_len].rstrip()
    return text or None


def _loads_card_json(raw: str) -> dict | None:
    text = (raw or "").strip()
    if not text:
        return None
    # 模型偶发中文引号
    text = (
        text.replace("“", '"')
        .replace("”", '"')
        .replace("‘", "'")
        .replace("’", "'")
    )
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # 宽松：单引号键值
        try:
            data = json.loads(
                text.replace("'", '"')
            )
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


# 供 MUTE 等伪指令复用
loads_marker_json = _loads_card_json


def parse_card_actions(raw_reply: str) -> CardParseResult:
    actions: list[CardAction] = []
    for match in _CARD_MARKER_RE.finditer(raw_reply or ""):
        data = _loads_card_json(match.group(1))
        if not data:
            continue
        uid_raw = data.get("user_id", data.get("userId"))
        try:
            user_id = int(uid_raw)
        except (TypeError, ValueError):
            continue
        card = data.get("card")
        if not isinstance(card, str):
            continue
        kind_raw = str(data.get("kind") or "playful").lower()
        kind: CardKind = "request" if kind_raw == "request" else "playful"
        actions.append(CardAction(user_id=user_id, card=card, kind=kind))

    cleaned = _CARD_MARKER_RE.sub("", raw_reply or "")
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return CardParseResult(text=cleaned, actions=actions)


def extract_at_user_ids(event: MessageEvent, *, self_id: int) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for seg in event.message:
        if seg.type != "at":
            continue
        qq = seg.data.get("qq")
        if qq in (None, "all"):
            continue
        try:
            uid = int(qq)
        except (TypeError, ValueError):
            continue
        if uid == self_id or uid in seen:
            continue
        seen.add(uid)
        ids.append(uid)
    return ids


def format_candidates_for_prompt(candidates: list[CardCandidate]) -> str:
    if not candidates:
        return ""
    lines = ["【成员候选】改名片/禁言只能针对下面这些人；user_id 必须从这里抄："]
    for c in candidates:
        lines.append(
            f"- {c.relation}: {c.display_name} user_id={c.user_id} "
            f"当前名片={c.current_card or '（空）'} 身份={c.role}"
        )
    return "\n".join(lines)


async def build_card_candidates(
    bot: Bot,
    event: GroupMessageEvent,
) -> list[CardCandidate]:
    candidates: list[CardCandidate] = []
    seen: set[int] = set()

    async def _add(uid: int, relation: str) -> None:
        if uid in seen:
            return
        seen.add(uid)
        try:
            info = await bot.get_group_member_info(
                group_id=event.group_id,
                user_id=uid,
                no_cache=True,
            )
        except Exception:  # noqa: BLE001
            logger.debug(f"dl_senpai: get_group_member_info failed for {uid}")
            info = {}
        if not isinstance(info, dict):
            info = {}
        display = (
            str(info.get("card") or "").strip()
            or str(info.get("nickname") or "").strip()
            or str(uid)
        )
        current = str(info.get("card") or "").strip()
        role = str(info.get("role") or "member")
        candidates.append(
            CardCandidate(
                user_id=uid,
                display_name=display,
                current_card=current,
                role=role,
                relation=relation,
            )
        )

    await _add(int(event.user_id), "sender")
    for uid in extract_at_user_ids(event, self_id=int(event.self_id)):
        await _add(uid, "at")
    return candidates


async def bot_can_manage_cards(bot: Bot, group_id: int) -> bool:
    try:
        info = await bot.get_group_member_info(
            group_id=group_id,
            user_id=int(bot.self_id),
            no_cache=True,
        )
    except Exception:  # noqa: BLE001
        logger.warning("dl_senpai: failed to check bot group role")
        return False
    if not isinstance(info, dict):
        return False
    return str(info.get("role") or "") in {"admin", "owner"}


def filter_actions(
    actions: list[CardAction],
    *,
    candidates: list[CardCandidate],
    max_len: int,
    allow_playful: bool,
    playful_ready: bool,
) -> list[CardAction]:
    by_id = {c.user_id: c for c in candidates}
    out: list[CardAction] = []
    seen: set[int] = set()
    playful_used = False
    for action in actions:
        cand = by_id.get(action.user_id)
        if cand is None:
            continue
        # 自己（sender）任意身份都可改；其他人仅普通成员
        if cand.relation != "sender" and cand.role != "member":
            continue
        if action.user_id in seen:
            continue
        card = sanitize_card(action.card, max_len=max_len)
        if card is None:
            continue
        if action.kind == "playful":
            if not allow_playful or not playful_ready or playful_used:
                continue
            playful_used = True
        seen.add(action.user_id)
        out.append(CardAction(user_id=action.user_id, card=card, kind=action.kind))
    return out


@dataclass(frozen=True)
class TitleApplyResult:
    ok: bool
    method: str = ""
    title: str = ""
    verified: bool = False


def title_apply_success_note(result: TitleApplyResult) -> str:
    if not result.ok or not result.verified:
        return ""
    if result.method == "card":
        return f"\n群名片已换成「{result.title}」啦～（专属头衔接口没反应，学姐改名片顶上）"
    return f"\n专属头衔已挂上「{result.title}」啦～点开群成员资料能看到"


def title_apply_failure_note() -> str:
    return (
        "\n（头衔这次好像没真正挂上……可能是 QQ/NapCat 接口抽风了，"
        "晚点再触发一次好感或让管理检查机器人有没有群管权限）"
    )


def sanitize_special_title(raw: str, *, max_len: int = 12) -> str | None:
    """群专属头衔（QQ 一般最长 12 字）。"""
    return sanitize_card(raw, max_len=max_len)


async def _read_special_title(bot: Bot, *, group_id: int, user_id: int) -> str | None:
    try:
        info = await bot.get_group_member_info(
            group_id=group_id,
            user_id=user_id,
            no_cache=True,
        )
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(info, dict):
        return None
    for key in ("title", "special_title"):
        value = str(info.get(key) or "").strip()
        if value:
            return value
    return ""


async def _try_special_title(
    bot: Bot,
    *,
    group_id: int,
    user_id: int,
    special_title: str,
) -> bool:
    try:
        await bot.call_api(
            "set_group_special_title",
            group_id=group_id,
            user_id=user_id,
            special_title=special_title,
            duration=-1,
        )
    except TypeError:
        try:
            await bot.set_group_special_title(
                group_id=group_id,
                user_id=user_id,
                special_title=special_title,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"dl_senpai set_group_special_title failed group={group_id} user={user_id}"
            )
            return False
    except Exception:  # noqa: BLE001
        logger.exception(
            f"dl_senpai set_group_special_title failed group={group_id} user={user_id}"
        )
        return False

    for delay in (0.0, 0.35, 0.8):
        if delay:
            await asyncio.sleep(delay)
        actual = await _read_special_title(bot, group_id=group_id, user_id=user_id)
        if actual == special_title:
            logger.info(
                f"dl_senpai set_group_special_title verified group={group_id} "
                f"user={user_id} title={special_title!r}"
            )
            return True

    actual = await _read_special_title(bot, group_id=group_id, user_id=user_id)
    logger.warning(
        f"dl_senpai set_group_special_title unverified group={group_id} "
        f"user={user_id} want={special_title!r} got={actual!r}"
    )
    return False


def _card_safe_for_managed_title(current_card: str, *, known_titles: frozenset[str]) -> bool:
    card = (current_card or "").strip()
    if not card:
        return True
    return card in known_titles


async def apply_group_title(
    bot: Bot,
    *,
    group_id: int,
    user_id: int,
    title: str,
    allow_card_fallback: bool = True,
    known_managed_titles: frozenset[str] | None = None,
) -> TitleApplyResult:
    special_title = sanitize_special_title(title)
    if not special_title:
        return TitleApplyResult(ok=False)

    if await _try_special_title(
        bot,
        group_id=group_id,
        user_id=user_id,
        special_title=special_title,
    ):
        return TitleApplyResult(
            ok=True,
            method="special",
            title=special_title,
            verified=True,
        )

    if not allow_card_fallback:
        return TitleApplyResult(ok=False, title=special_title)

    current_card = await _read_card(bot, group_id=group_id, user_id=user_id) or ""
    known = known_managed_titles or frozenset()
    if not _card_safe_for_managed_title(current_card, known_titles=known):
        logger.warning(
            f"dl_senpai title card fallback skipped group={group_id} user={user_id} "
            f"card={current_card!r}"
        )
        return TitleApplyResult(ok=False, title=special_title)

    try:
        await bot.set_group_card(
            group_id=group_id,
            user_id=user_id,
            card=special_title,
        )
    except Exception:  # noqa: BLE001
        logger.exception(
            f"dl_senpai title card fallback failed group={group_id} user={user_id}"
        )
        return TitleApplyResult(ok=False, title=special_title)

    for delay in (0.0, 0.35):
        if delay:
            await asyncio.sleep(delay)
        actual = await _read_card(bot, group_id=group_id, user_id=user_id)
        if actual == special_title:
            logger.info(
                f"dl_senpai title card fallback verified group={group_id} "
                f"user={user_id} card={special_title!r}"
            )
            return TitleApplyResult(
                ok=True,
                method="card",
                title=special_title,
                verified=True,
            )

    actual = await _read_card(bot, group_id=group_id, user_id=user_id)
    logger.warning(
        f"dl_senpai title card fallback unverified group={group_id} "
        f"user={user_id} want={special_title!r} got={actual!r}"
    )
    return TitleApplyResult(ok=False, title=special_title)


async def apply_special_title(
    bot: Bot,
    *,
    group_id: int,
    user_id: int,
    title: str,
    allow_card_fallback: bool = True,
    known_managed_titles: frozenset[str] | None = None,
) -> bool:
    result = await apply_group_title(
        bot,
        group_id=group_id,
        user_id=user_id,
        title=title,
        allow_card_fallback=allow_card_fallback,
        known_managed_titles=known_managed_titles,
    )
    return result.ok and result.verified


async def _read_card(bot: Bot, *, group_id: int, user_id: int) -> str | None:
    try:
        info = await bot.get_group_member_info(
            group_id=group_id,
            user_id=user_id,
            no_cache=True,
        )
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(info, dict):
        return None
    return str(info.get("card") or "").strip()


async def apply_card_actions(
    bot: Bot,
    *,
    group_id: int,
    actions: list[CardAction],
) -> list[CardAction]:
    applied: list[CardAction] = []
    for action in actions:
        try:
            await bot.set_group_card(
                group_id=group_id,
                user_id=action.user_id,
                card=action.card,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                f"dl_senpai set_group_card failed group={group_id} user={action.user_id}"
            )
            continue

        # QQ / NapCat 回读常有延迟：接口成功即视为已提交；回读不符只打警告
        actual = await _read_card(bot, group_id=group_id, user_id=action.user_id)
        if actual != action.card:
            await asyncio.sleep(0.35)
            actual = await _read_card(bot, group_id=group_id, user_id=action.user_id)
        if actual != action.card:
            logger.warning(
                f"dl_senpai set_group_card pending/lag group={group_id} "
                f"user={action.user_id} want={action.card!r} got={actual!r} "
                f"(API ok, client may refresh later)"
            )

        applied.append(action)
        logger.info(
            f"dl_senpai set_group_card group={group_id} "
            f"user={action.user_id} card={action.card!r} kind={action.kind}"
        )
    return applied


def action_summary_for_memory(actions: list[CardAction]) -> str:
    # 不写进可见记忆，避免模型把「改名片日志」学进正文并泄漏
    return ""


def card_failure_note() -> str:
    return "（等等，名片好像没改上——可能是权限或接口抽风了，你再说一次要改成啥，学姐再试。）"


def card_missing_marker_note() -> str:
    return "（名片这边还没真正改成哦，你明确说下「改成xxx」，学姐再帮你弄一次。）"
