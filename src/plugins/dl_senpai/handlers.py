
import re

from nonebot import logger, on_message, on_notice
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupDecreaseNoticeEvent,
    GroupIncreaseNoticeEvent,
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
    NoticeEvent,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11 import Message as OBMessage

from .config import PluginConfig, get_config
from .memory import (
    ChatMemory,
    session_id_for_group,
    session_id_for_private,
)
from .welcome import build_welcome_text
from .group_verify import (
    build_verify_pass_text,
    build_verify_prompt,
    get_verify_store,
    looks_like_verify_message,
)
from .group_features import is_feature_enabled
from .observability import trace_scope
from .persona import get_senpai_name
from .style import polish_reply
from .reply_send import send_text_reply
from .stickers import get_sticker_store
from .group_card import apply_group_title, bot_can_manage_cards, title_apply_failure_note, title_apply_success_note
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
from .affection import all_affection_tier_titles, affection_scope_private, format_affection_line, format_gain_footnote, format_help as format_affection_help, format_leaderboard as format_affection_leaderboard, format_status as format_affection_status, get_affection_store, is_private_affection_scope, parse_affection_command
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
def _get_memory(config: PluginConfig | None = None) -> ChatMemory:
    global _memory
    cfg = config or get_config()
    if _memory is None:
        _memory = ChatMemory(cfg.memory_dir, max_history=cfg.max_history)
    return _memory


_facade_singleton = None


# -- Phase 6A：共享 Tool Dispatch --

_dispatcher_singleton = None


def _get_shared_dispatcher():
    """取共享 ToolDispatcher 单例。

    Phase 6A 第九/十节：legacy **必须**复用与 RUNTIME 相同的
    ToolExecutor，禁止另建 LegacyToolExecutor。
    """
    global _dispatcher_singleton
    if _dispatcher_singleton is None:
        from .agent.tool_dispatch import SharedToolDispatcher

        _dispatcher_singleton = SharedToolDispatcher()
    return _dispatcher_singleton




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
        bubble_total_max_chars=config.bubble_total_max_chars,
    )


# [Phase 3 停用] welcome_matcher = on_notice(priority=5, block=False)   # 已由 gateway_matcher + Router 接管
# [Phase 3 停用] leave_matcher = on_notice(priority=5, block=False)   # 已由 gateway_matcher + Router 接管


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


# [Phase 3 停用] verify_matcher = on_message(Rule(_verify_rule), priority=4, block=True)   # 已由 gateway_matcher + Router 接管


# [Phase 3] 不再注册到 NoneBot；函数体保留给 Router 适配层调用
# @welcome_matcher.handle()
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


# [Phase 3] 不再注册到 NoneBot；函数体保留给 Router 适配层调用
# @leave_matcher.handle()
async def handle_group_decrease(bot: Bot, event: GroupDecreaseNoticeEvent) -> None:
    """退群/被踢：清掉待认证，避免空转踢人任务。"""
    config = get_config()
    if not config.verify_enable:
        return
    if event.user_id == event.self_id:
        return
    get_verify_store(config.verify_dir).drop(event.group_id, event.user_id)


# [Phase 3] 不再注册到 NoneBot；函数体保留给 Router 适配层调用
# @verify_matcher.handle()
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


# —— Phase 3：以下旧 matcher 已停用，改由 gateway_matcher + Router 接管 ——
#
# 保留变量名与 handle 函数（`handle_checkin` / `handle_shop` 等），因为
# Router 的 dispatch_* 适配层仍在调用它们。**只是不再注册到 NoneBot**。
#
# 停用而非删除的理由：这些 handler 是经过真实群聊验证的业务实现，
# 删除会让 Router 适配层失去目标。等 Phase 4 把业务搬进独立 plugin 后，
# 再连同适配层一起清理。
#
# 对应关系（详见 Phase 3 报告的 matcher 迁移表）：
#   verify_matcher    → Command stage / command_registry / guard_verify
#   checkin_matcher   → Command stage / command_registry / guard_checkin
#   shop_matcher      → Command stage / command_registry / guard_shop
#   private_matcher   → Agent stage / AgentTriggerPolicy(PRIVATE)
#   mention_matcher   → Agent stage / AgentTriggerPolicy(MENTION)
#   interrupt_matcher → Agent stage / AgentTriggerPolicy(INTERRUPT)
#   sticker_collector → Observer / sticker_collector
#   welcome_matcher   → Observer / welcome_notice
#   leave_matcher     → Observer / leave_notice

_LEGACY_MATCHERS_DISABLED = True


# [Phase 3] 不再注册到 NoneBot；函数体保留给 Router 适配层调用
# @sticker_collector.handle()
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


# [Phase 3 停用] checkin_matcher = on_message(Rule(_checkin_rule), priority=5, block=True)   # 已由 gateway_matcher + Router 接管


# [Phase 3] 不再注册到 NoneBot；函数体保留给 Router 适配层调用
# @checkin_matcher.handle()
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


# [Phase 3 停用] shop_matcher = on_message(Rule(_shop_rule), priority=5, block=True)   # 已由 gateway_matcher + Router 接管


# [Phase 3] 不再注册到 NoneBot；函数体保留给 Router 适配层调用
# @shop_matcher.handle()
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
# [Phase 3 停用] private_matcher = on_message(Rule(...), priority=8, block=True)
#   已由 AgentTriggerPolicy(PRIVATE) 接管


# [Phase 3] 不再注册到 NoneBot；函数体保留给 Router 适配层调用
# @private_matcher.handle()
async def handle_private(bot: Bot, event: PrivateMessageEvent) -> None:
    config = get_config()
    if not config.enable_private:
        return
    if await _handle_affection_command(bot, event, config=config):
        return
    await _handle_chat(bot, event, force_mention=True)


# —— 被 @ 必回 ——
# [Phase 3 停用] mention_matcher = on_message(rule=to_me(), priority=10, block=True)   # 已由 gateway_matcher + Router 接管


# [Phase 3] 不再注册到 NoneBot；函数体保留给 Router 适配层调用
# @mention_matcher.handle()
async def handle_mention(bot: Bot, event: MessageEvent) -> None:
    await _handle_chat(bot, event, force_mention=True)


# —— 低概率插嘴（未 @）——
# [Phase 3 停用] interrupt_matcher = on_message(priority=50, block=False)   # 已由 gateway_matcher + Router 接管


# [Phase 3] 不再注册到 NoneBot；函数体保留给 Router 适配层调用
# @interrupt_matcher.handle()
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

    # 建立 trace 上下文：本轮消息的所有阶段（LLM/工具/记忆/发送）共用
    # 同一个 trace_id，日志里可以整条链路 grep 出来。
    # 这里只建立上下文，不重写任何既有日志语句（Phase 2 范围约定）。
    with trace_scope(
        session_id=session_id,
        message_id=str(getattr(event, "message_id", "") or ""),
        stage="chat",
        user_id=str(getattr(event, "user_id", "") or ""),
        group_id=str(getattr(event, "group_id", "") or ""),
        force_mention=force_mention,
    ):
        await _handle_chat_inner(bot, event, force_mention=force_mention, config=config)


async def _handle_chat_inner(
    bot: Bot,
    event: MessageEvent,
    *,
    force_mention: bool,
    config: PluginConfig,
) -> None:
    """**Forwarding wrapper**（Phase 6.5 第八节）。

    真实实现已搬到 `agent/legacy_orchestrator.py` 的
    `LegacyOrchestrator.run()`。本函数只做 argument 适配。

    保留它的原因：兼容层与测试都按这个名字引用
    （例如 test_phase55_acceptance 的 monkeypatch）。

    禁止在这里重新加入：
        prompt 组装 / LLM 调用 / intent 判定 / tool 逻辑 / memory 逻辑
    """
    from .agent.legacy_orchestrator import LegacyOrchestrator

    await LegacyOrchestrator().run(
        bot,
        event,
        force_mention=force_mention,
        config=config,
    )


# ============================================================
# Phase 3：Gateway matcher（Router 接管的唯一入口）
# ============================================================
#
# Phase 3 的核心变化：**从 7 个 NoneBot matcher 收敛为 1 个**。
#
# 重构前（Phase 1 审计）：
#     verify(4) welcome/leave(5) checkin(5) shop(5)
#     private(8) mention(10) interrupt(50) sticker(100)
#   —— "谁处理这条消息"由 priority 数字隐式决定，散落在 handlers.py。
#
# 现在：
#     gateway_matcher(1)  →  gateway.from_onebot_event()  →  Router
#                                                        ├ Command（Registry）
#                                                        ├ Plugin
#                                                        └ Agent（trigger + legacy bridge）
#
# 本 matcher **不做任何业务判断**，只负责：把 OneBot 事件归一化 → 交给 Router。
#
# 关于两个 matcher：
#   - `gateway_notice_matcher` 处理 notice（入群/退群），只走观察者；
#   - `gateway_message_matcher` 处理消息，走观察者 + Router。
# 分开是因为 notice 事件没有"回复"语义，混在一起会让 Router 里到处是类型判断。

gateway_notice_matcher = on_notice(priority=1, block=False)
gateway_message_matcher = on_message(priority=1, block=False)


async def _dispatch_via_router(bot: Bot, event: MessageEvent) -> None:
    """把一条 OneBot 事件交给新 Router 处理。

    这是 Phase 3 唯一的调度入口。`block=False` 是刻意的：即使 Router 判定
    "这条消息不需要回复"，也不应该阻止其他插件（如果有）看到它。
    """
    from .gateway import from_onebot_event
    from .core.runtime import get_runtime

    ctx = from_onebot_event(event, bot)
    if ctx is None:
        return
    try:
        runtime = get_runtime()
        await runtime.dispatch(ctx)
    except Exception:  # noqa: BLE001 — 调度失败不应让整条链路静默
        logger.exception("dl_senpai gateway dispatch failed")


@gateway_notice_matcher.handle()
async def handle_gateway_notice(bot: Bot, event: NoticeEvent) -> None:
    """通知类事件：只走旁路观察者（入群欢迎 / 退群清理）。"""
    from .gateway import from_onebot_event

    ctx = from_onebot_event(event, bot)
    if ctx is None:
        return
    try:
        from .core.runtime import get_runtime

        await get_runtime().router.dispatch_observers(ctx)
    except Exception:  # noqa: BLE001
        logger.exception("dl_senpai gateway notice dispatch failed")


@gateway_message_matcher.handle()
async def handle_gateway_message(bot: Bot, event: MessageEvent) -> None:
    """消息事件：观察者（永不短路）+ Router（Command/Plugin/Agent）。"""
    await _dispatch_via_router(bot, event)
#
# 本节把「Router 决定调用谁」与「业务怎么执行」接起来。
#
# 设计原则：这些函数是**薄适配器**——只从 MessageContext 取回 raw_event，
# 然后调用既有的 `handle_*` 实现。业务逻辑一行不变。
#
# 之所以能这么做，是因为 `handle_checkin` / `handle_shop` 等函数本身
# 已经是"输入 (bot, event) → 输出副作用"的纯执行器，不关心自己被谁调用。
# Phase 3 要改的正是"谁调用它们"，因此适配成本极低。


def _ctx_bot_event(ctx):
    """从 MessageContext 取回 (bot, raw_event)。

    新代码不应依赖 raw_event；这里是**唯一**的转换点，让 Router/Command
    保持平台无关，而 legacy 业务继续用 OneBot 事件。
    """
    return getattr(ctx, "bot", None), getattr(ctx, "raw_event", None)


async def dispatch_checkin_command(ctx, match) -> None:
    """签到 / 好感度命令的 Router 入口。

    `match.name` 区分是签到域还是好感度域，但两者共用同一个
    `handle_checkin` 实现（原 matcher 也是同一函数处理两者）。
    """
    bot, event = _ctx_bot_event(ctx)
    if bot is None or event is None:
        return
    if not isinstance(event, GroupMessageEvent):
        return
    await handle_checkin(bot, event)


async def dispatch_shop_command(ctx, match) -> None:
    """商店命令的 Router 入口。"""
    bot, event = _ctx_bot_event(ctx)
    if bot is None or event is None:
        return
    if not isinstance(event, GroupMessageEvent):
        return
    await handle_shop(bot, event)


async def dispatch_verify_command(ctx, match) -> None:
    """新人认证命令的 Router 入口。"""
    bot, event = _ctx_bot_event(ctx)
    if bot is None or event is None:
        return
    if not isinstance(event, GroupMessageEvent):
        return
    await handle_group_verify(bot, event)


async def observe_stickers(ctx) -> None:
    """表情采集观察者。

    这是 Phase 3 「Observer ≠ Router Plugin」的样板：它**永不短路**，
    因此即使"签到"命令已经 consume 了这条消息，它仍然会被执行
    （观察者在 Router.dispatch 之前独立跑）。

    行为与旧的 `sticker_collector` matcher（priority=100, block=False）等价。
    """
    bot, event = _ctx_bot_event(ctx)
    if bot is None or event is None:
        return
    config = get_config()
    resolved = _resolve_session(event, config)
    if resolved is None:
        return
    session_id, _ = resolved
    await _collect_stickers(
        event,
        session_id=session_id,
        context_text=_plain_text(event),
        source_user=_sender_name(event),
        config=config,
        bot=bot,
    )


async def observe_recent_chat(ctx) -> None:
    """最近聊天观察者：把**每一条**群/私聊消息记进当天流水。

    为什么要独立于 Router：她只回被 @ 的那条，但「你怎么看」的质量取决于
    前面那段她没参与的对话。观察者永不短路，所以即使这条消息被命令 consume
    了，也照样进流水。

    为什么不用 context.history 顶替：那里只有她参与过的轮次，别人互聊的内容
    从来没有进过任何存储。

    失败静默：记流水是旁路行为，绝不能因为它出错就拦掉正常回复。
    """
    try:
        bot, event = _ctx_bot_event(ctx)
        if bot is None or event is None:
            return
        config = get_config()
        if not getattr(config, "recent_chat_enable", True):
            return
        resolved = _resolve_session(event, config)
        if resolved is None:
            return
        session_id, _is_group = resolved

        # 她自己的消息不进流水：那些已经以 assistant 身份在对话历史里，
        # 再记一遍会在背景段里和自己重复。
        self_id = str(getattr(event, "self_id", "") or "")
        user_id = str(getattr(event, "user_id", "") or "")
        if self_id and user_id == self_id:
            return

        text = _plain_text(event)
        if not text:
            return

        from .recent_chat import get_recent_chat_store

        store = get_recent_chat_store(getattr(config, "recent_chat_dir", None))
        store.append(
            session_id,
            sender=_sender_name(event),
            user_id=user_id,
            text=text,
            message_id=str(getattr(event, "message_id", "") or ""),
        )
        # 顺手尝试落盘。flush 自己按 flush_interval 节流（默认 5 秒一次），
        # 所以每条消息都调用并不会造成写放大——但能保证数据在进程被杀之前
        # 最迟 5 秒内落盘，不必依赖退出钩子。
        store.flush()

        # 群风格采样：和流水共用这个「永不短路」的观察者点。
        # 为什么不挂 agent 路径：她只回被 @ 的那条，而「本群怎么说话」恰恰
        # 藏在那些她没参与的对话里；挂 agent 路径上只能采到被触发的消息。
        if _is_group and getattr(config, "group_style_enable", True):
            style_dir = getattr(config, "group_style_dir", "") or ""
            group_id = str(getattr(event, "group_id", "") or "")
            if style_dir and group_id:
                try:
                    from .group_style import get_group_style_store
                    from .persona_filter import neutralize as _pf_neutralize

                    # 过一道过滤器：别人贴的角色设定（猫娘/傲娇/凛）不该被当成
                    # 「本群说话风格」学进去——那些词会通过风格提示词绕回来。
                    style_text, style_hit = _pf_neutralize(text)
                    if not style_hit:
                        get_group_style_store(style_dir).observe(group_id, style_text)
                except Exception:  # noqa: BLE001 — 采样是旁路，坏了也不能拦消息
                    logger.debug("dl_senpai group_style observe failed", exc_info=True)
    except Exception:  # noqa: BLE001
        logger.debug("dl_senpai recent_chat observe failed", exc_info=True)


async def observe_notice_welcome(ctx) -> None:
    """入群欢迎/认证注册观察者（通知类事件）。"""
    bot, event = _ctx_bot_event(ctx)
    if bot is None or event is None:
        return
    from nonebot.adapters.onebot.v11 import GroupIncreaseNoticeEvent

    if isinstance(event, GroupIncreaseNoticeEvent):
        await handle_group_increase(bot, event)


async def observe_notice_leave(ctx) -> None:
    """退群清理观察者（通知类事件）。"""
    bot, event = _ctx_bot_event(ctx)
    if bot is None or event is None:
        return
    from nonebot.adapters.onebot.v11 import GroupDecreaseNoticeEvent

    if isinstance(event, GroupDecreaseNoticeEvent):
        await handle_group_decrease(bot, event)


# -- 命令 guard：把原来的 Rule 条件搬到这里 -------------------------------
# guard 与正则分离是刻意的：正则属于「命令语法」（静态、可枚举），
# 群白名单/功能开关属于「运行时是否生效」（动态）。
#
# 所有 guard 都接受一个可选 `config` 覆盖：Router 正常运行时用全局缓存配置，
# shadow 验证与测试可以注入一份独立配置。这让"路由决策"变成可复现的纯函数，
# 而不依赖进程全局状态。


def _guard_config(ctx, override=None):
    """取 guard 用的配置：优先显式覆盖，其次 ctx.extra，最后全局。"""
    if override is not None:
        return override
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict) and extra.get("config") is not None:
        return extra["config"]
    return get_config()


def guard_checkin(ctx, config=None) -> bool:
    """签到/好感度命令的生效条件（等价于旧 `_checkin_rule` 的前两段）。"""
    if not getattr(ctx, "group_id", ""):
        return False
    cfg = _guard_config(ctx, config)
    return is_feature_enabled(cfg, ctx.group_id, "checkin")


def guard_shop(ctx, config=None) -> bool:
    """商店命令的生效条件（等价于旧 `_shop_rule`）。"""
    if not getattr(ctx, "group_id", ""):
        return False
    cfg = _guard_config(ctx, config)
    if not is_feature_enabled(cfg, ctx.group_id, "shop"):
        return False
    return cfg.is_checkin_group(ctx.group_id)


def guard_verify(ctx, config=None) -> bool:
    """认证命令的生效条件（等价于旧 `_verify_rule`）。

    除了群/开关，还要求该用户**确实处于待认证状态**——
    否则"认证"两个字会被当成普通聊天。
    """
    gid = getattr(ctx, "group_id", "")
    if not gid:
        return False
    cfg = _guard_config(ctx, config)
    if not cfg.is_verify_group(gid):
        return False
    if not is_feature_enabled(cfg, gid, "verify"):
        return False
    store = get_verify_store(cfg.verify_dir)
    if not store.is_pending(gid, getattr(ctx, "user_id", "")):
        return False
    return looks_like_verify_message(getattr(ctx, "plain_text", "") or "")
