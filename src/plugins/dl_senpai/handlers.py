from __future__ import annotations

import re

from nonebot import logger, on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11 import Message as OBMessage
from nonebot.rule import to_me, Rule

from .config import PluginConfig, get_config
from .llm import LLMError, get_llm
from .memory import (
    ChatMemory,
    session_id_for_group,
    session_id_for_private,
)
from .memory_recall import messages_for_llm_with_recall
from .inner_state import (
    build_inner_state_brief,
    get_inner_state_store,
    interrupt_prob_multiplier,
    scope_key as inner_scope_key,
)
from .group_style import build_group_style_brief, get_group_style_store
from .trigger import AttentionWindow, InterruptCooldown, decide_trigger
from .welcome import build_welcome_text
from .group_verify import (
    build_verify_pass_text,
    build_verify_prompt,
    get_verify_store,
    looks_like_verify_message,
)
from .group_features import is_feature_enabled, observe_group
from .persona import get_senpai_name
from .style import polish_reply, strip_unapplied_mute_claims
from .reply_send import send_text_reply, _send_image_with_retry
from .reply_quote import build_chat_text_with_reply, resolve_reply_message, user_intent_text
from .stickers import get_sticker_store, maybe_pick_sticker_reply, extract_sticker_segments
from .voice import (
    build_voice_segment,
    extract_voice_quote_from_user,
    looks_like_voice_request,
    text_for_voice,
)
from .image_gen import (
    DrawCooldown,
    build_image_segment,
    classify_senpai_draw,
    draw_block_note,
    draw_failure_note,
    draw_retry_hint,
    parse_draw_request,
    resolve_draw_prompt,
    strip_draw_markers,
    wants_senpai_image,
)
from .browser_agent import (
    BrowseCooldown,
    browse_block_note,
    browse_failure_note,
    browse_retry_hint,
    browser_configured,
    parse_browse_request,
    paths_to_image_segments,
    resolve_browse_tasks,
    run_browse_tasks,
    strip_browse_markers,
    wants_browser_help,
)
from .vision import (
    extract_vision_image_segments,
    format_user_text_for_chat,
    format_user_text_for_memory,
    resolve_vision_images,
)
from .group_card import (
    CardCooldown,
    action_summary_for_memory,
    apply_card_actions,
    apply_group_title,
    bot_can_manage_cards,
    build_card_candidates,
    card_failure_note,
    card_missing_marker_note,
    extract_at_user_ids,
    filter_actions,
    format_candidates_for_prompt,
    looks_like_card_request,
    title_apply_failure_note,
    title_apply_success_note,
)
from .group_mute import (
    MuteCooldown,
    apply_mute_actions,
    can_emit_mute,
    filter_mute_actions,
    format_mute_ack,
    looks_like_mute_request,
    mute_block_note,
    mute_summary_for_memory,
    resolve_explicit_mute,
)
from .trends import get_trends_brief, get_hot_items
from .checkin import (
    TITLE_MILESTONES,
    format_checkin_reply,
    format_help,
    format_leaderboard,
    format_status,
    format_task_view,
    get_checkin_store,
    parse_checkin_command,
)
from .person_memory import (
    ImpressionUpdate,
    build_person_memory_brief,
    extract_traits_from_user_text,
    get_person_memory_store,
)
from .affection import (
    all_affection_tier_titles,
    affection_scope_private,
    build_affection_brief,
    format_affection_line,
    format_gain_footnote,
    format_help as format_affection_help,
    format_leaderboard as format_affection_leaderboard,
    format_status as format_affection_status,
    get_affection_store,
    is_private_affection_scope,
    parse_affection_command,
)
from .shop import (
    format_shop_help,
    get_shop_store,
    handle_shop_open,
    handle_shop_purchase,
    handle_shop_warehouse,
    handle_shop_wear,
    parse_shop_command,
)
import random as _random


def _fe(config: PluginConfig, event: MessageEvent, key: str) -> bool:
    """按群功能开关：群消息查该群覆盖，私聊只看全局。"""
    gid = event.group_id if isinstance(event, GroupMessageEvent) else None
    return is_feature_enabled(config, gid, key)


def _affection_enabled(config: PluginConfig, group_id: int | str) -> bool:
    return is_feature_enabled(config, group_id, "affection") and config.is_checkin_group(group_id)


def _affection_scope(config: PluginConfig, event: MessageEvent) -> str | None:
    if not config.affection_enable:
        return None
    if isinstance(event, GroupMessageEvent):
        if config.is_checkin_group(event.group_id):
            return str(event.group_id)
        return None
    if isinstance(event, PrivateMessageEvent):
        if config.enable_private:
            return affection_scope_private(event.user_id)
        return None
    return None


async def _handle_affection_command(
    bot: Bot,
    event: MessageEvent,
    *,
    config: PluginConfig,
) -> bool:
    aff_cmd = parse_affection_command(event.get_plaintext())
    if aff_cmd == "none":
        return False
    scope = _affection_scope(config, event)
    if scope is None:
        return False

    name = _sender_name(event)
    aff_store = get_affection_store(config.affection_dir)
    private_mode = is_private_affection_scope(scope)

    if aff_cmd == "help":
        await _reply(
            bot,
            event,
            polish_reply(format_affection_help(private_mode=private_mode)),
        )
        return True

    if aff_cmd == "status":
        aff_user = aff_store.get_user(scope, event.user_id)
        await _reply(
            bot,
            event,
            polish_reply(
                format_affection_status(aff_user, name=name, private_mode=private_mode)
            ),
        )
        return True

    if aff_cmd == "leaderboard":
        if private_mode:
            await _reply(
                bot,
                event,
                polish_reply(
                    "私聊没有群排行哦～去群里发「好感排行」看看，或者发「好感度」看自己。"
                ),
            )
            return True
        board = aff_store.leaderboard(scope, limit=10)
        await _reply(
            bot,
            event,
            polish_reply(format_affection_leaderboard(board)),
        )
        return True

    return False


def _shop_affection_value(config: PluginConfig, group_id: int | str, user_id: int | str) -> int | None:
    if not _affection_enabled(config, group_id):
        return None
    return get_affection_store(config.affection_dir).get_user(group_id, user_id).value


def _with_affection_footnote(text: str, result, *, always_show: bool = True) -> str:
    foot = format_gain_footnote(result, always_show=always_show)
    if not foot:
        return text
    return f"{text}\n{foot}"


async def _maybe_sync_affection_title(
    bot: Bot,
    *,
    group_id: int,
    user_id: int,
    result,
    config: PluginConfig,
) -> str:
    if not (
        result.title_changed
        and result.user
        and result.user.tier_title
        and config.affection_sync_special_title
    ):
        return ""
    if not await bot_can_manage_cards(bot, group_id):
        return ""
    applied = await apply_group_title(
        bot,
        group_id=group_id,
        user_id=user_id,
        title=result.user.tier_title,
        allow_card_fallback=config.group_title_fallback_card,
        known_managed_titles=all_affection_tier_titles(),
    )
    if applied.ok and applied.verified:
        return title_apply_success_note(applied)
    return title_apply_failure_note()


async def _finalize_affection_gain(
    bot: Bot,
    *,
    scope: str,
    user_id: int,
    text: str,
    result,
    config: PluginConfig,
    always_show: bool = True,
    group_id: int | None = None,
) -> str:
    text = _with_affection_footnote(text, result, always_show=always_show)
    if group_id is not None and not is_private_affection_scope(scope):
        text += await _maybe_sync_affection_title(
            bot,
            group_id=group_id,
            user_id=user_id,
            result=result,
            config=config,
        )
    return text

_memory: ChatMemory | None = None
_cooldown = InterruptCooldown()
_attention = AttentionWindow()
_card_cooldown = CardCooldown()
_mute_cooldown = MuteCooldown()
_draw_cooldown = DrawCooldown()
_browse_cooldown = BrowseCooldown()

def _get_memory(config: PluginConfig | None = None) -> ChatMemory:
    global _memory
    cfg = config or get_config()
    if _memory is None:
        _memory = ChatMemory(cfg.memory_dir, max_history=cfg.max_history)
    return _memory


def _plain_text(event: MessageEvent) -> str:
    return event.get_plaintext().strip()


def _sender_name(event: MessageEvent) -> str:
    if isinstance(event, GroupMessageEvent):
        info = event.sender
        return (
            (info.card if info and info.card else None)
            or (info.nickname if info else None)
            or str(event.user_id)
        )
    info = event.sender
    return (info.nickname if info and info.nickname else None) or str(event.user_id)


async def _collect_stickers(
    event: MessageEvent,
    *,
    session_id: str,
    context_text: str,
    source_user: str,
    config: PluginConfig,
    bot: Bot | None = None,
) -> None:
    if not _fe(config, event, "sticker") or not config.sticker_collect:
        return
    try:
        added = await get_sticker_store(config).collect_from_event(
            event,
            session_id=session_id,
            context_text=context_text,
            source_user=source_user,
            bot=bot,
        )
        if added:
            total = get_sticker_store(config).count(session_id)
            logger.info(
                f"dl_senpai collected {added} sticker(s) in {session_id} (total={total})"
            )
    except Exception:  # noqa: BLE001
        logger.exception("dl_senpai sticker collect failed")


def _resolve_session(event: MessageEvent, config: PluginConfig) -> tuple[str, bool] | None:
    if isinstance(event, GroupMessageEvent):
        if not config.is_group_allowed(event.group_id):
            return None
        return session_id_for_group(event.group_id), True
    if isinstance(event, PrivateMessageEvent):
        if not config.enable_private:
            return None
        return session_id_for_private(event.user_id), False
    return None


def _is_to_me(event: MessageEvent) -> bool:
    return event.is_tome()


async def _reply(
    bot: Bot,
    event: MessageEvent,
    text: str,
    *,
    sticker: MessageSegment | None = None,
    voice: MessageSegment | None = None,
    image: MessageSegment | None = None,
) -> None:
    config = get_config()
    await send_text_reply(
        bot,
        event,
        text,
        sticker=sticker,
        voice=voice,
        image=image,
        reply_max_chars=config.reply_max_chars,
        reply_chunk_chars=config.reply_chunk_chars,
        nickname=get_senpai_name(),
        humanize_send=bool(config.humanize_send),
        typing_delay_ms_min=config.typing_delay_ms_min,
        typing_delay_ms_max=config.typing_delay_ms_max,
        bubble_max=config.bubble_max,
        bubble_min_chars=config.bubble_min_chars,
        bubble_prefer_chars=config.bubble_prefer_chars,
    )


welcome_matcher = on_notice(priority=5, block=False)
leave_matcher = on_notice(priority=5, block=False)


async def _verify_rule(event: GroupMessageEvent, bot: Bot) -> bool:
    config = get_config()
    if not config.is_verify_group(event.group_id):
        return False
    if not is_feature_enabled(config, event.group_id, "verify"):
        return False
    store = get_verify_store(config.verify_dir)
    if not store.is_pending(event.group_id, event.user_id):
        return False
    if not (event.is_tome() or _message_ats_bot(event, bot)):
        return False
    return looks_like_verify_message(event.get_plaintext() or "")


verify_matcher = on_message(Rule(_verify_rule), priority=4, block=True)


@welcome_matcher.handle()
async def handle_group_increase(bot: Bot, event: GroupIncreaseNoticeEvent) -> None:
    config: PluginConfig = get_config()
    if not config.is_group_allowed(event.group_id):
        return
    if event.user_id == event.self_id:
        return

    nickname = str(event.user_id)
    try:
        info = await bot.get_group_member_info(
            group_id=event.group_id,
            user_id=event.user_id,
        )
        nickname = (
            (info.get("card") if isinstance(info, dict) else None)
            or (info.get("nickname") if isinstance(info, dict) else None)
            or nickname
        )
    except Exception:  # noqa: BLE001
        logger.warning("dl_senpai: failed to fetch new member info")

    parts: list[str] = []
    if is_feature_enabled(config, event.group_id, "welcome"):
        parts.append(build_welcome_text(str(nickname)))

    if config.is_verify_group(event.group_id) and is_feature_enabled(
        config, event.group_id, "verify"
    ):
        timeout = max(30, int(config.verify_timeout_sec))
        store = get_verify_store(config.verify_dir)
        item = store.register(
            group_id=event.group_id,
            user_id=event.user_id,
            nickname=str(nickname),
            timeout_sec=timeout,
        )
        store.schedule_kick(bot, item)
        mins = max(1, (timeout + 59) // 60)
        prompt = build_verify_prompt(timeout_min=mins)
        # 欢迎词已含「认证」+时限时，不再叠一句
        welcome = parts[0] if parts else ""
        if "认证" not in welcome or "分钟" not in welcome:
            parts.append(prompt)

    if not parts:
        return
    text = "\n".join(parts)
    await bot.send(event, OBMessage(MessageSegment.at(event.user_id) + " " + text))


@leave_matcher.handle()
async def handle_group_decrease(bot: Bot, event: GroupDecreaseNoticeEvent) -> None:
    """退群/被踢：清掉待认证，避免空转踢人任务。"""
    config = get_config()
    if not config.verify_enable:
        return
    if event.user_id == event.self_id:
        return
    get_verify_store(config.verify_dir).drop(event.group_id, event.user_id)


@verify_matcher.handle()
async def handle_group_verify(bot: Bot, event: GroupMessageEvent) -> None:
    config = get_config()
    store = get_verify_store(config.verify_dir)
    item = store.mark_passed(event.group_id, event.user_id)
    nick = (item.nickname if item else "") or _sender_name(event)
    await bot.send(
        event,
        OBMessage(MessageSegment.at(event.user_id) + " " + build_verify_pass_text(nick)),
    )


def _message_ats_bot(event: MessageEvent, bot: Bot) -> bool:
    self_id = str(getattr(bot, "self_id", "") or "")
    for seg in event.message:
        if seg.type != "at":
            continue
        qq = str(seg.data.get("qq") or "")
        if qq and qq == self_id:
            return True
    return False


# —— 被动收集群聊/私聊里的表情包 ——
sticker_collector = on_message(priority=100, block=False)


@sticker_collector.handle()
async def handle_sticker_collect(bot: Bot, event: MessageEvent) -> None:
    config = get_config()
    resolved = _resolve_session(event, config)
    if resolved is None:
        return
    session_id, _ = resolved
    text = _plain_text(event)
    await _collect_stickers(
        event,
        session_id=session_id,
        context_text=text,
        source_user=_sender_name(event),
        config=config,
        bot=bot,
    )


# —— 签到（仅白名单群，优先于普通聊天）——
def _checkin_rule(event: MessageEvent) -> bool:
    if not isinstance(event, GroupMessageEvent):
        return False
    cfg = get_config()
    if not is_feature_enabled(cfg, event.group_id, "checkin"):
        return False
    text = event.get_plaintext().strip()
    # 去掉开头对自己的 @ 后再识别（to_me 时纯文本可能已无 at）
    return (
        parse_checkin_command(text) != "none"
        or parse_affection_command(text) != "none"
    )


checkin_matcher = on_message(Rule(_checkin_rule), priority=5, block=True)


@checkin_matcher.handle()
async def handle_checkin(bot: Bot, event: GroupMessageEvent) -> None:
    config = get_config()
    if not is_feature_enabled(config, event.group_id, "checkin"):
        return
    cmd = parse_checkin_command(event.get_plaintext())
    aff_cmd = parse_affection_command(event.get_plaintext())
    if cmd == "none" and aff_cmd == "none":
        return

    name = _sender_name(event)
    store = get_checkin_store(config.checkin_dir)
    session_id = session_id_for_group(event.group_id)

    if aff_cmd == "help":
        text = format_affection_help()
        if cmd == "help":
            text = f"{format_help()}\n\n{text}"
        await _reply(bot, event, polish_reply(text))
        return

    if aff_cmd == "status" and _affection_enabled(config, event.group_id):
        aff_user = get_affection_store(config.affection_dir).get_user(
            event.group_id, event.user_id
        )
        await _reply(
            bot,
            event,
            polish_reply(format_affection_status(aff_user, name=name)),
        )
        return

    if aff_cmd == "leaderboard" and _affection_enabled(config, event.group_id):
        board = get_affection_store(config.affection_dir).leaderboard(
            event.group_id, limit=10
        )
        await _reply(
            bot,
            event,
            polish_reply(format_affection_leaderboard(board)),
        )
        return

    if cmd == "none":
        return

    if cmd == "help":
        text = format_help()
        if _affection_enabled(config, event.group_id):
            text = f"{text}\n\n{format_affection_help()}"
        await _reply(bot, event, polish_reply(text))
        return

    if cmd == "status":
        user = store.get_user(event.group_id, event.user_id)
        text = format_status(user, name=name)
        if _affection_enabled(config, event.group_id):
            aff_user = get_affection_store(config.affection_dir).get_user(
                event.group_id, event.user_id
            )
            text = f"{text}\n{format_affection_line(aff_user.value)}"
        await _reply(bot, event, polish_reply(text))
        return

    if cmd == "leaderboard_points":
        board = store.leaderboard(event.group_id, by="points", limit=10)
        await _reply(bot, event, polish_reply(format_leaderboard(board, by="points")))
        return

    if cmd == "leaderboard_streak":
        board = store.leaderboard(event.group_id, by="streak", limit=10)
        await _reply(bot, event, polish_reply(format_leaderboard(board, by="streak")))
        return

    if cmd == "task_view":
        user = store.get_user(event.group_id, event.user_id)
        await _reply(
            bot,
            event,
            polish_reply(format_task_view(name=name, task=user.pending_task)),
        )
        return

    if cmd == "task_done":
        ok, msg, _user = store.complete_task(event.group_id, event.user_id)
        if ok and _affection_enabled(config, event.group_id):
            aff = get_affection_store(config.affection_dir).add(
                event.group_id,
                event.user_id,
                config.affection_gain_task,
                display_name=name,
            )
            msg = await _finalize_affection_gain(
                bot,
                scope=str(event.group_id),
                user_id=event.user_id,
                text=msg,
                result=aff,
                config=config,
                group_id=event.group_id,
            )
        if ok:
            await _reply(bot, event, polish_reply(msg))
        else:
            await _reply(
                bot,
                event,
                polish_reply(
                    f"📝 任务\n{name}，{msg}"
                ),
            )
        return

    # checkin
    result = store.checkin(
        event.group_id,
        event.user_id,
        display_name=name,
    )
    text = format_checkin_reply(result, name=name)
    if result.ok and _affection_enabled(config, event.group_id):
        aff = get_affection_store(config.affection_dir).add(
            event.group_id,
            event.user_id,
            config.affection_gain_checkin,
            display_name=name,
        )
        text = await _finalize_affection_gain(
            bot,
            scope=str(event.group_id),
            user_id=event.user_id,
            text=text,
            result=aff,
            config=config,
            group_id=event.group_id,
        )
    sticker = None
    if result.ok and is_feature_enabled(config, event.group_id, "sticker"):
        if _random.random() < max(0.0, min(1.0, config.checkin_sticker_prob)):
            try:
                record = get_sticker_store(config).pick(
                    session_id,
                    user_text="签到",
                    reply_text=text,
                    reply_prob=1.0,
                )
                if record is not None:
                    sticker = record.to_segment()
            except Exception:  # noqa: BLE001
                logger.debug("dl_senpai checkin sticker pick failed")

    if (
        result.ok
        and result.title_upgraded
        and result.title
        and config.checkin_sync_special_title
        and await bot_can_manage_cards(bot, event.group_id)
    ):
        applied = await apply_group_title(
            bot,
            group_id=event.group_id,
            user_id=event.user_id,
            title=result.title,
            allow_card_fallback=config.group_title_fallback_card,
            known_managed_titles=frozenset(name for _need, name in TITLE_MILESTONES),
        )
        if applied.ok and applied.verified:
            text += title_apply_success_note(applied)
        else:
            text += title_apply_failure_note()

    # 先发签到文字（保证成功）；表情包另发，失败不影响签到
    await _reply(bot, event, polish_reply(text), sticker=None)
    if sticker is not None:
        try:
            await bot.send(event, OBMessage(sticker))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"dl_senpai checkin sticker send failed: {exc}")


# —— 积分服装店（签到群）——
def _shop_rule(event: MessageEvent) -> bool:
    if not isinstance(event, GroupMessageEvent):
        return False
    cfg = get_config()
    if not is_feature_enabled(cfg, event.group_id, "shop"):
        return False
    if not cfg.is_checkin_group(event.group_id):
        return False
    text = event.get_plaintext().strip()
    return parse_shop_command(text) != "none"


shop_matcher = on_message(Rule(_shop_rule), priority=5, block=True)


@shop_matcher.handle()
async def handle_shop(bot: Bot, event: GroupMessageEvent) -> None:
    config = get_config()
    if not is_feature_enabled(config, event.group_id, "shop"):
        return
    if not config.is_checkin_group(event.group_id):
        return
    cmd = parse_shop_command(event.get_plaintext())
    if cmd == "none":
        return

    name = _sender_name(event)
    checkin_store = get_checkin_store(config.checkin_dir)
    shop_store = get_shop_store(config.shop_dir)

    if cmd == "help":
        await _reply(bot, event, polish_reply(format_shop_help()))
        return

    user = checkin_store.get_user(event.group_id, event.user_id)

    if cmd == "open":
        text, image = await handle_shop_open(
            event.group_id,
            user_id=event.user_id,
            points=user.points,
            affection_value=_shop_affection_value(config, event.group_id, event.user_id),
            store=shop_store,
            config=config,
        )
        await _reply(bot, event, polish_reply(text), image=image)
        return

    if cmd == "warehouse":
        text = handle_shop_warehouse(
            event.group_id,
            event.user_id,
            shop_store,
            user_name=name,
        )
        await _reply(bot, event, polish_reply(text))
        return

    if cmd == "wear":
        result = await handle_shop_wear(
            event.group_id,
            event.user_id,
            event.get_plaintext(),
            display_name=name,
            store=shop_store,
            config=config,
        )
        msg = result.message
        if result.ok and _affection_enabled(config, event.group_id):
            aff = get_affection_store(config.affection_dir).add(
                event.group_id,
                event.user_id,
                config.affection_gain_shop_wear,
                display_name=name,
            )
            msg = await _finalize_affection_gain(
                bot,
                scope=str(event.group_id),
                user_id=event.user_id,
                text=msg,
                result=aff,
                config=config,
                group_id=event.group_id,
            )
        await _reply(
            bot,
            event,
            polish_reply(msg),
            image=result.outfit_image,
        )
        return

    # buy
    result = await handle_shop_purchase(
        event.group_id,
        event.user_id,
        event.get_plaintext(),
        display_name=name,
        store=shop_store,
        checkin_store=checkin_store,
        config=config,
        affection_value=_shop_affection_value(config, event.group_id, event.user_id),
    )
    msg = result.message
    if result.ok and _affection_enabled(config, event.group_id):
        aff = get_affection_store(config.affection_dir).add(
            event.group_id,
            event.user_id,
            config.affection_gain_shop_buy,
            display_name=name,
        )
        msg = await _finalize_affection_gain(
            bot,
            scope=str(event.group_id),
            user_id=event.user_id,
            text=msg,
            result=aff,
            config=config,
            group_id=event.group_id,
        )
    await _reply(
        bot,
        event,
        polish_reply(msg),
        image=result.outfit_image,
    )


# —— 私聊：每条消息都当作直接找学姐 ——
private_matcher = on_message(
    Rule(lambda event: isinstance(event, PrivateMessageEvent)),
    priority=8,
    block=True,
)


@private_matcher.handle()
async def handle_private(bot: Bot, event: PrivateMessageEvent) -> None:
    config = get_config()
    if not config.enable_private:
        return
    if await _handle_affection_command(bot, event, config=config):
        return
    await _handle_chat(bot, event, force_mention=True)


# —— 被 @ 必回 ——
mention_matcher = on_message(rule=to_me(), priority=10, block=True)


@mention_matcher.handle()
async def handle_mention(bot: Bot, event: MessageEvent) -> None:
    await _handle_chat(bot, event, force_mention=True)


# —— 低概率插嘴（未 @）——
interrupt_matcher = on_message(priority=50, block=False)


@interrupt_matcher.handle()
async def handle_interrupt(bot: Bot, event: MessageEvent) -> None:
    if event.is_tome():
        return
    await _handle_chat(bot, event, force_mention=False)


async def _handle_chat(
    bot: Bot,
    event: MessageEvent,
    *,
    force_mention: bool,
) -> None:
    config: PluginConfig = get_config()
    resolved = _resolve_session(event, config)
    if resolved is None:
        return
    session_id, _ = resolved

    if isinstance(event, GroupMessageEvent):
        observe_group(event.group_id)

    text = _plain_text(event)
    await _collect_stickers(
        event,
        session_id=session_id,
        context_text=text,
        source_user=_sender_name(event),
        config=config,
        bot=bot,
    )
    sticker_segments = extract_sticker_segments(event.message)
    sticker_count = len(sticker_segments)
    reply_message = await resolve_reply_message(bot, event)
    photo_segments: list[dict] = []
    if _fe(config, event, "vision"):
        photo_segments.extend(extract_vision_image_segments(event.message))
        if reply_message is not None:
            photo_segments.extend(extract_vision_image_segments(reply_message))
    image_urls: list[str] = []
    if photo_segments:
        image_urls = await resolve_vision_images(
            bot,
            photo_segments,
            max_images=config.vision_max_images,
        )
    has_images = bool(photo_segments) or bool(image_urls)
    image_count = len(photo_segments)
    chat_text = format_user_text_for_chat(
        text,
        image_count=image_count,
        sticker_count=sticker_count,
    )
    # 引用别人消息再 @学姐：把引用内容 + 自己说的一起给模型
    chat_text = build_chat_text_with_reply(event, chat_text, reply_message=reply_message)
    intent_text = user_intent_text(text, chat_text)

    # QQ 很难同条消息又 @ 又发表情：刚找过学姐后短时间内发的表情，也算找她
    followup_sticker = False
    if (
        not force_mention
        and not _is_to_me(event)
        and sticker_count > 0
        and isinstance(event, GroupMessageEvent)
        and _attention.active(
            session_id,
            event.user_id,
            config.sticker_followup_sec,
        )
    ):
        force_mention = True
        followup_sticker = True
        if chat_text.startswith("（发了个表情"):
            chat_text = chat_text.replace("（发了个表情", "（接着找学姐，发了个表情", 1)
        elif chat_text:
            chat_text = f"（接着找学姐）{chat_text}"
        else:
            chat_text = "（接着找学姐，发了个表情）"

    if force_mention and not chat_text and not has_images and sticker_count == 0:
        chat_text = f"{get_senpai_name()}～"
    if not chat_text and not force_mention and not has_images and sticker_count == 0:
        return

    # 纯表情包、未 @ 且不在跟进窗口：只被动收集，不插嘴、不调模型
    if (
        sticker_count > 0
        and not text.strip()
        and not has_images
        and not force_mention
    ):
        return

    is_mentioned = (
        force_mention
        or _is_to_me(event)
        or isinstance(event, PrivateMessageEvent)
    )

    # 群风格采样：凡进到聊天处理的群消息都记一笔（含未触发回复的）
    if (
        _fe(config, event, "group_style")
        and isinstance(event, GroupMessageEvent)
        and (text or "").strip()
    ):
        try:
            get_group_style_store(config.group_style_dir).observe(
                event.group_id, text or intent_text
            )
        except Exception:  # noqa: BLE001
            logger.warning("dl_senpai group_style observe failed")

    inner_key = "global"
    interrupt_scale = 1.0
    inner_state_brief = ""
    if _fe(config, event, "inner_state"):
        try:
            inner_key = inner_scope_key(
                per_group=bool(config.inner_state_per_group),
                group_id=event.group_id if isinstance(event, GroupMessageEvent) else None,
                private=isinstance(event, PrivateMessageEvent),
            )
            istore = get_inner_state_store(config.inner_state_dir)
            istate = istore.load(inner_key)
            interrupt_scale = interrupt_prob_multiplier(istate)
            inner_state_brief = build_inner_state_brief(istate)
        except Exception:  # noqa: BLE001
            logger.warning("dl_senpai inner_state load failed")

    decision = decide_trigger(
        is_mentioned=is_mentioned,
        text=text or chat_text,
        session_id=session_id,
        interrupt_prob=config.interrupt_prob,
        cooldown_sec=config.interrupt_cooldown,
        min_msg_len=config.min_msg_len,
        keyword_boost=config.keyword_boost,
        cooldown=_cooldown,
        has_images=has_images,
        image_boost=config.image_interrupt_boost,
        interrupt_prob_scale=interrupt_scale,
    )

    if decision.kind == "none":
        return

    # 本轮已明确找学姐：武装窗口，方便下一条表情包接上
    if decision.kind == "mention" and isinstance(event, GroupMessageEvent):
        _attention.arm(session_id, event.user_id)
        if followup_sticker:
            logger.info(
                f"dl_senpai sticker follow-up as mention "
                f"session={session_id} user={event.user_id}"
            )

    interrupt = decision.kind == "interrupt"
    if interrupt:
        _cooldown.mark(session_id)

    if not config.api_configured():
        if is_mentioned:
            await _reply(
                bot,
                event,
                polish_reply("稍等一会啦，学姐这边还没收拾好，晚点再来找我哦～"),
            )
        return

    if has_images and not image_urls:
        if is_mentioned:
            await _reply(
                bot,
                event,
                polish_reply(
                    "图学姐有点看不清呢……你再发一次，或者把文字也打出来好不好？"
                ),
            )
        return

    sender = _sender_name(event)
    memory = _get_memory(config)
    raw_history = memory.load_all(session_id)
    history, memory_recall_brief = messages_for_llm_with_recall(
        raw_history,
        intent_text or chat_text,
        recent_turns=config.memory_recent_turns,
        recall_top=config.memory_recall_top,
        enable=_fe(config, event, "memory_recall"),
    )
    llm = get_llm(config)

    group_style_brief = ""
    if _fe(config, event, "group_style") and isinstance(event, GroupMessageEvent):
        try:
            group_style_brief = build_group_style_brief(
                get_group_style_store(config.group_style_dir),
                event.group_id,
                top_n=config.group_style_top_n,
                min_count=config.group_style_min_count,
                short=bool(interrupt),
            )
        except Exception:  # noqa: BLE001
            logger.warning("dl_senpai group_style brief failed")

    card_enable = False
    mute_enable = False
    draw_enable = False
    browser_enable = False
    card_prompt = ""
    candidates = []
    need_mod = False
    local_infer = config.is_local_infer()
    if local_infer:
        # 本地微调模型吃不消工具指令，开着就会把候选名单/系统提示原样发出去
        logger.info("dl_senpai local infer: disable card/mute/draw/browser/voice extras")
    if isinstance(event, GroupMessageEvent) and not local_infer:
        if _fe(config, event, "card") and (not interrupt or config.card_allow_on_interrupt):
            need_mod = True
        if _fe(config, event, "mute") and (not interrupt or config.mute_allow_on_interrupt):
            need_mod = True
    if need_mod:
        if await bot_can_manage_cards(bot, event.group_id):
            candidates = await build_card_candidates(bot, event)
            if candidates:
                card_prompt = format_candidates_for_prompt(candidates)
                if _fe(config, event, "card") and (
                    not interrupt or config.card_allow_on_interrupt
                ):
                    card_enable = True
                if _fe(config, event, "mute") and (
                    not interrupt or config.mute_allow_on_interrupt
                ):
                    mute_enable = True
    if (
        not local_infer
        and _fe(config, event, "draw")
        and config.draw_configured()
        and (not interrupt or config.draw_allow_on_interrupt)
    ):
        draw_enable = True
    if (
        not local_infer
        and _fe(config, event, "browser")
        and browser_configured(config)
        and (not interrupt or config.browser_allow_on_interrupt)
    ):
        browser_enable = True

    trends_brief = ""
    trend_titles: list[str] = []
    if _fe(config, event, "trends"):
        try:
            hot_items = await get_hot_items(limit=config.trends_max_items)
            trend_titles = [i.title for i in hot_items if i.title]
            trends_brief = await get_trends_brief(
                max_items=config.trends_max_items,
                max_chars=config.trends_prompt_max_chars,
                interrupt=interrupt,
            )
        except Exception:  # noqa: BLE001
            logger.warning("dl_senpai trends brief failed")

    affection_brief = ""
    aff_scope = _affection_scope(config, event)
    if aff_scope and is_mentioned and not interrupt:
        aff_user = get_affection_store(config.affection_dir).get_user(
            aff_scope, event.user_id
        )
        affection_brief = build_affection_brief(name=sender, value=aff_user.value)

    person_memory_brief = ""
    person_memory_enable = _fe(config, event, "person_memory")
    if person_memory_enable:
        pm_store = get_person_memory_store(
            config.person_memory_dir,
            max_traits=config.person_memory_max_traits,
            max_note_chars=config.person_memory_max_note_chars,
        )
        # 每次互动都记下当前昵称，便于跨群认出同一个人
        profile = pm_store.touch_name(event.user_id, sender)
        person_memory_brief = build_person_memory_brief(
            profile,
            name=sender,
            query=intent_text or chat_text,
        )

    try:
        import time as _time

        t0 = _time.perf_counter()
        logger.info(
            f"dl_senpai LLM call session={session_id} "
            f"interrupt={interrupt} text_len={len(chat_text)}"
        )
        private_chat = isinstance(event, PrivateMessageEvent)
        result = await llm.chat(
            history,
            chat_text,
            interrupt=interrupt,
            sender_name=sender,
            image_urls=image_urls or None,
            card_enable=card_enable,
            card_candidates_prompt=card_prompt,
            mute_enable=mute_enable,
            trends_brief=trends_brief,
            trend_titles=trend_titles,
            affection_brief=affection_brief,
            person_memory_brief=person_memory_brief,
            person_memory_enable=person_memory_enable,
            draw_enable=draw_enable,
            browser_enable=browser_enable,
            private_chat=private_chat,
            plain_user_text=intent_text,
            search_enable=_fe(config, event, "search"),
            fewshot_enable=bool(config.fewshot_enable),
            inner_state_brief=inner_state_brief if config.inner_state_enable else "",
            group_style_brief=group_style_brief,
            memory_recall_brief=memory_recall_brief,
        )
        was_searched = result.searched
        # 小模型常口头说「改好了」却漏写 <<<CARD>>>：意图明确时补救一轮
        if (
            card_enable
            and not interrupt
            and not result.card_actions
            and looks_like_card_request(intent_text)
        ):
            logger.info(
                f"dl_senpai card intent without marker, retry once "
                f"session={session_id}"
            )
            hist2 = [
                *history,
                {"role": "user", "content": f"{sender}：{chat_text}"},
                {"role": "assistant", "content": result.text},
            ]
            result = await llm.chat(
                hist2,
                (
                    "你刚才只口头说改了名片，但没写 <<<CARD ...>>>，所以实际没改成。"
                    "请重新简短回复，并在末尾补上指令行；user_id 必须从成员候选抄，"
                    'kind 用 request。格式：<<<CARD {"user_id":数字,"card":"新名片","kind":"request"}>>>'
                ),
                interrupt=False,
                sender_name="系统",
                card_enable=True,
                card_candidates_prompt=card_prompt,
                mute_enable=False,
            )
        # 仅「明确要求禁言」且漏写 <<<MUTE>>> 时补救；辱骂不等于要求禁言
        if (
            mute_enable
            and not interrupt
            and not result.mute_actions
            and looks_like_mute_request(intent_text)
        ):
            logger.info(
                f"dl_senpai mute intent without marker, retry once "
                f"session={session_id}"
            )
            hist2 = [
                *history,
                {"role": "user", "content": f"{sender}：{chat_text}"},
                {"role": "assistant", "content": result.text},
            ]
            result = await llm.chat(
                hist2,
                (
                    "对方在要求禁言。请简短回复一两句（别问心事、别说对方发不了消息），"
                    "并在末尾补上指令行；user_id 必须从成员候选抄（禁言我=sender 的 user_id）。"
                    '格式：<<<MUTE {"user_id":数字,"duration":60,"kind":"request"}>>>'
                ),
                interrupt=False,
                sender_name="系统",
                card_enable=False,
                card_candidates_prompt=card_prompt,
                mute_enable=True,
            )
        # 明确要求生图却漏写 <<<DRAW>>> 时补救一轮
        if (
            draw_enable
            and not interrupt
            and not result.draw_force
            and wants_senpai_image(intent_text)
        ):
            logger.info(
                f"dl_senpai draw intent without marker, retry once "
                f"session={session_id}"
            )
            hist2 = [
                *history,
                {"role": "user", "content": f"{sender}：{chat_text}"},
                {"role": "assistant", "content": result.text},
            ]
            retry = await llm.chat(
                hist2,
                draw_retry_hint(intent_text),
                interrupt=False,
                sender_name="系统",
                draw_enable=True,
                private_chat=private_chat,
            )
            was_searched = was_searched or retry.searched
            result = retry
        if (
            browser_enable
            and not interrupt
            and not result.browse_force
            and wants_browser_help(intent_text)
        ):
            logger.info(
                f"dl_senpai browse intent without marker, retry once "
                f"session={session_id}"
            )
            hist2 = [
                *history,
                {"role": "user", "content": f"{sender}：{chat_text}"},
                {"role": "assistant", "content": result.text},
            ]
            browse_retry = await llm.chat(
                hist2,
                browse_retry_hint(intent_text),
                interrupt=False,
                sender_name="系统",
                browser_enable=True,
                private_chat=private_chat,
            )
            was_searched = was_searched or browse_retry.searched
            result = browse_retry
        elapsed = _time.perf_counter() - t0
        logger.info(
            f"dl_senpai LLM ok session={session_id} "
            f"reply_len={len(result.text)} "
            f"card_actions={len(result.card_actions)} "
            f"mute_actions={len(result.mute_actions)} "
            f"searched={was_searched or result.searched} elapsed={elapsed:.1f}s"
        )
    except LLMError as e:
        logger.warning(f"dl_senpai LLM error: {e}")
        if is_mentioned:
            await _reply(bot, event, str(e))
        return
    except Exception as e:  # noqa: BLE001
        logger.exception(f"dl_senpai unexpected error: {e}")
        if is_mentioned:
            await _reply(
                bot,
                event,
                polish_reply("稍等一会啦，学姐现在有些事情要做噢～晚点再叫我吧"),
            )
        return

    reply = result.text
    if "<<<DRAW" in reply.upper():
        leaked = parse_draw_request(reply)
        reply = strip_draw_markers(reply)
        if leaked.prompt and not result.draw_force:
            result.draw_prompt = leaked.prompt
            result.draw_force = True
    if "<<<BROWSE" in reply.upper():
        leaked_browse = parse_browse_request(reply)
        reply = strip_browse_markers(reply)
        if leaked_browse.tasks and not result.browse_force:
            result.browse_tasks = leaked_browse.tasks
            result.browse_force = True
    image_request = bool(
        draw_enable and (result.draw_force or wants_senpai_image(intent_text))
    )
    browse_intent = browser_enable and wants_browser_help(intent_text)
    browse_wanted = browser_enable and (
        result.browse_force or browse_intent
    )
    applied_cards = []
    applied_mutes = []
    card_intent = card_enable and looks_like_card_request(intent_text)
    if card_enable and result.card_actions and isinstance(event, GroupMessageEvent):
        # 硬门槛：闲聊（晚安/QWQ）里模型乱写 CARD 一律不执行
        if not card_intent:
            logger.warning(
                f"dl_senpai drop hallucinated card actions "
                f"session={session_id} n={len(result.card_actions)} "
                f"text={chat_text!r}"
            )
            # 顺带清掉「已经改成xxx」的嘴炮，避免晚安被说成改名片
            reply = polish_reply(
                re.sub(
                    r"[^。！？\n]{0,40}群名片已经改成[^。！？\n]{0,30}",
                    "",
                    reply,
                )
            )
        else:
            playful_ready = _card_cooldown.ready(
                session_id, config.card_playful_cooldown
            )
            actions = filter_actions(
                result.card_actions,
                candidates=candidates,
                max_len=config.card_max_len,
                allow_playful=True,
                playful_ready=playful_ready,
            )
            if actions:
                applied_cards = await apply_card_actions(
                    bot,
                    group_id=event.group_id,
                    actions=actions,
                )
                if any(a.kind == "playful" for a in applied_cards):
                    _card_cooldown.mark(session_id)
            if result.card_actions and not applied_cards:
                logger.warning(
                    f"dl_senpai card actions dropped/failed "
                    f"session={session_id} raw={len(result.card_actions)} "
                    f"filtered={len(actions)} applied=0"
                )
                reply = polish_reply(f"{reply}\n{card_failure_note()}")
    elif card_intent and not result.card_actions:
        logger.warning(
            f"dl_senpai card intent but still no marker session={session_id}"
        )
        reply = polish_reply(f"{reply}\n{card_missing_marker_note()}")

    if mute_enable and isinstance(event, GroupMessageEvent) and can_emit_mute(intent_text):
        mute_ready = _mute_cooldown.ready(session_id, config.mute_cooldown)
        mute_actions = list(result.mute_actions)
        block_reason = ""

        # 模型仍漏指令时：本地解析「禁言我 / @禁言」
        if not mute_actions:
            at_ids = extract_at_user_ids(event, self_id=int(event.self_id))
            explicit, block_reason = resolve_explicit_mute(
                chat_text,
                sender_id=int(event.user_id),
                at_ids=at_ids,
                candidates=candidates,
                duration=max(config.mute_min_sec, min(60, config.mute_max_sec)),
            )
            if explicit is not None:
                mute_actions = [explicit]
                logger.info(
                    f"dl_senpai mute explicit fallback "
                    f"user={explicit.user_id} duration={explicit.duration}"
                )

        if mute_actions:
            mutes, filter_reason = filter_mute_actions(
                mute_actions,
                candidates=candidates,
                max_duration=config.mute_max_sec,
                min_duration=config.mute_min_sec,
                ready=mute_ready,
            )
            if not mute_ready:
                block_reason = "cooldown"
            elif filter_reason:
                block_reason = filter_reason
            if mutes:
                applied_mutes = await apply_mute_actions(
                    bot,
                    group_id=event.group_id,
                    actions=mutes,
                )
                if applied_mutes:
                    _mute_cooldown.mark(session_id)
                    ack = format_mute_ack(applied_mutes)
                    if ack:
                        reply = polish_reply(f"{reply}\n{ack}")
                    block_reason = ""
                else:
                    block_reason = "failed"

        if not applied_mutes:
            reply = strip_unapplied_mute_claims(reply)
            # 仅明确禁言请求失败时提示；辱骂未禁言不必贴系统说明
            if looks_like_mute_request(intent_text):
                note = mute_block_note(block_reason or "no_marker")
                if note:
                    reply = polish_reply(f"{reply}\n{note}")
            logger.warning(
                f"dl_senpai mute not applied session={session_id} "
                f"reason={block_reason or 'no_marker'} "
                f"raw={len(result.mute_actions)}"
            )
    elif mute_enable and result.mute_actions:
        logger.warning(
            f"dl_senpai drop hallucinated mute actions "
            f"session={session_id} n={len(result.mute_actions)}"
        )
        reply = strip_unapplied_mute_claims(reply)

    # 本轮没真正禁成：清掉「已经帮你禁言了」嘴炮，避免重复念
    if not applied_mutes:
        reply = polish_reply(strip_unapplied_mute_claims(reply))

    user_stored = format_user_text_for_memory(
        sender,
        text,
        image_count=image_count,
        sticker_count=sticker_count,
    )
    # 有引用时记忆也存完整上下文，避免下轮忘掉「在回哪句」
    if getattr(event, "reply", None) is not None and chat_text:
        user_stored = f"{sender}：{chat_text}"

    sticker = maybe_pick_sticker_reply(
        session_id,
        user_text=chat_text,
        reply_text=reply,
        interrupt=interrupt,
        config=config,
        force=bool(result.sticker_force),
        extra_query=result.sticker_query or "",
    ) if (not local_infer and not image_request and not browse_wanted) else None
    if sticker is None and result.sticker_force and not local_infer:
        logger.info(
            f"dl_senpai wanted sticker ({result.sticker_query!r}) "
            f"but collection empty for {session_id}"
        )

    voice = None
    voice_override = result.voice_override
    voice_wanted = (
        not local_infer
        and _fe(config, event, "voice")
        and not interrupt
        and (
            result.voice_force
            or looks_like_voice_request(intent_text)
            or (
                len(reply) <= config.voice_max_chars
                and _random.random()
                < max(0.0, min(1.0, config.voice_reply_prob))
            )
        )
    )
    if voice_wanted:
        if not voice_override and looks_like_voice_request(intent_text):
            voice_override = extract_voice_quote_from_user(intent_text)
        try:
            voice = await build_voice_segment(
                reply,
                config=config,
                force=bool(
                    result.voice_force or looks_like_voice_request(intent_text)
                ),
                override=voice_override,
                user_request=intent_text,
                interrupt=interrupt,
            )
        except Exception:  # noqa: BLE001
            logger.warning("dl_senpai voice build failed", exc_info=True)

    image = None
    draw_prompt = ""
    draw_scene = classify_senpai_draw(intent_text)
    draw_intent = draw_enable and wants_senpai_image(intent_text)
    draw_wanted = (
        draw_enable
        and (
            result.draw_force
            or draw_intent
        )
    )
    if draw_wanted:
        draw_ready = _draw_cooldown.ready(session_id, config.draw_cooldown)
        draw_prompt = resolve_draw_prompt(
            marker_prompt=result.draw_prompt,
            user_request=intent_text,
            reply_text=reply,
            max_chars=config.draw_max_prompt_chars,
            group_id=event.group_id if isinstance(event, GroupMessageEvent) else None,
        )
        if not draw_ready:
            note = draw_block_note("cooldown", scene=draw_scene)
            if note:
                reply = polish_reply(f"{reply}\n{note}")
        elif not draw_prompt:
            note = draw_block_note("empty_prompt", scene=draw_scene)
            if note and draw_intent:
                reply = polish_reply(f"{reply}\n{note}")
        else:
            try:
                image = await build_image_segment(
                    resolved_prompt=draw_prompt,
                    user_request=intent_text,
                    reply_text=reply,
                    config=config,
                    interrupt=interrupt,
                    archive_session_id=session_id,
                    archive_source_user=sender,
                    archive_source_user_id=str(event.user_id),
                    archive_scene=draw_scene,
                )
                if image is not None:
                    _draw_cooldown.mark(session_id)
                elif draw_intent or result.draw_force:
                    reply = polish_reply(f"{reply}\n{draw_failure_note()}")
            except Exception:  # noqa: BLE001
                logger.warning("dl_senpai draw build failed", exc_info=True)
                if draw_intent or result.draw_force:
                    reply = polish_reply(f"{reply}\n{draw_failure_note()}")

    browse_images: list[MessageSegment] = []
    if browse_wanted:
        browse_ready = _browse_cooldown.ready(session_id, config.browser_cooldown)
        browse_tasks = resolve_browse_tasks(
            marker_tasks=list(result.browse_tasks),
            user_request=intent_text,
        )
        if not browse_ready:
            note = browse_block_note("cooldown")
            if note:
                reply = polish_reply(f"{reply}\n{note}")
        elif not browse_tasks:
            if browse_intent or result.browse_force:
                reply = polish_reply(f"{reply}\n{browse_failure_note()}")
        else:
            try:
                browse_result = await run_browse_tasks(browse_tasks, config=config)
                if browse_result.ok and browse_result.image_paths:
                    browse_images = paths_to_image_segments(browse_result.image_paths)
                    _browse_cooldown.mark(session_id)
                    if browse_result.page_url and browse_result.page_url not in reply:
                        reply = polish_reply(
                            f"{reply}\n（来源：{browse_result.page_url}）"
                        )
                elif browse_intent or result.browse_force:
                    reply = polish_reply(f"{reply}\n{browse_failure_note()}")
            except Exception:  # noqa: BLE001
                logger.warning("dl_senpai browse build failed", exc_info=True)
                if browse_intent or result.browse_force:
                    reply = polish_reply(f"{reply}\n{browse_failure_note()}")

    assistant_stored = reply if sticker is None else f"{reply} [学姐发了一个表情]"
    if voice is not None:
        spoken = text_for_voice(
            reply,
            max_chars=config.voice_max_chars,
            override=voice_override,
        )
        if spoken:
            assistant_stored += f" [学姐发了一条语音：{spoken}]"
        else:
            assistant_stored += " [学姐发了一条语音]"
    if image is not None:
        preview = (draw_prompt or "").strip()
        if len(preview) > 80:
            preview = preview[:79] + "…"
        if preview:
            assistant_stored += f" [学姐发了一张图：{preview}]"
        else:
            assistant_stored += " [学姐发了一张图]"
    if browse_images:
        assistant_stored += f" [学姐发了{len(browse_images)}张网页截图]"
    assistant_stored += action_summary_for_memory(applied_cards)
    assistant_stored += mute_summary_for_memory(applied_mutes)
    memory.append_turn(session_id, user_stored, assistant_stored)

    if (
        person_memory_enable
        and (not interrupt or config.person_memory_allow_on_interrupt)
    ):
        updates = list(result.impression_updates or [])
        # 模型漏写指令时：用户明确自我介绍仍记一笔
        if not updates:
            auto_traits = extract_traits_from_user_text(intent_text)
            if auto_traits:
                updates = [ImpressionUpdate(add=auto_traits, remove=[], notes=None)]
                logger.info(
                    f"dl_senpai person_memory heuristic traits={auto_traits!r} "
                    f"user={event.user_id}"
                )
        if updates:
            try:
                get_person_memory_store(
                    config.person_memory_dir,
                    max_traits=config.person_memory_max_traits,
                    max_note_chars=config.person_memory_max_note_chars,
                ).apply_updates(
                    event.user_id,
                    updates,
                    display_name=sender,
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    f"dl_senpai person_memory apply failed user={event.user_id}"
                )

    aff_scope = _affection_scope(config, event)
    if aff_scope and is_mentioned and not interrupt:
        aff = get_affection_store(config.affection_dir).add_chat(
            aff_scope,
            event.user_id,
            config.affection_gain_chat,
            display_name=sender,
        )
        group_id = event.group_id if isinstance(event, GroupMessageEvent) else None
        reply = polish_reply(
            await _finalize_affection_gain(
                bot,
                scope=aff_scope,
                user_id=event.user_id,
                text=reply,
                result=aff,
                config=config,
                always_show=False,
                group_id=group_id,
            )
        )
    bundled_browse = browse_images[0] if browse_images and image is None else None
    extra_browse = browse_images[1:] if bundled_browse else browse_images
    await _reply(
        bot,
        event,
        reply,
        sticker=sticker,
        voice=voice,
        image=image or bundled_browse,
    )
    for seg in extra_browse:
        await _send_image_with_retry(bot, event, seg)

    if config.inner_state_enable:
        try:
            get_inner_state_store(config.inner_state_dir).apply_after_reply(
                inner_key,
                reply_len=len(reply or ""),
                mentioned=bool(is_mentioned),
                interrupt=bool(interrupt),
                private_chat=isinstance(event, PrivateMessageEvent),
            )
        except Exception:  # noqa: BLE001
            logger.warning("dl_senpai inner_state update failed")
