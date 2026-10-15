"""共享领域 helper：功能开关、好感度作用域、贴纸采集。

Phase 6.6 第十六节 B 类（Shared domain helpers）。

这些 helper 同时被**命令处理器**与 **Legacy 编排**使用，
因此不属于 platform（不是纯平台操作），也不属于 legacy
（命令也要用）。抽到 `core/` 下与 router / session 同级。

依赖方向：

    handlers ──┐
               ├──→ core.domain ──→ config / stickers
    legacy  ───┘

**为什么放在 core/**：`core/` 已是运行时编排层（event_bus / router /
commands / plugins / session），这些是同一层的领域支撑函数。
不新建 `services/` 或 `shared/` 目录 —— 第十七节禁止为了归类
而创建垃圾桶式模块。
"""

from __future__ import annotations

from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    MessageEvent,
    PrivateMessageEvent,
)

from ..config import PluginConfig
from ..group_features import is_feature_enabled


def feature_enabled(config: PluginConfig, event: MessageEvent, key: str) -> bool:
    """按群功能开关：群消息查该群覆盖，私聊只看全局。

    原 handlers._fe —— **逐字搬运**。被贴纸采集与 legacy 编排共用。
    """
    gid = event.group_id if isinstance(event, GroupMessageEvent) else None
    return is_feature_enabled(config, gid, key)


def affection_scope(config: PluginConfig, event: MessageEvent) -> str | None:
    """好感度作用域：群（需为签到群）或私聊；不启用则返回 None。

    原 handlers._affection_scope —— **逐字搬运**。
    """
    from ..affection import affection_scope_private

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


async def collect_stickers(
    event: MessageEvent,
    *,
    session_id: str,
    context_text: str,
    source_user: str,
    config: PluginConfig,
    bot: Bot | None = None,
) -> None:
    """采集消息中的贴纸入库。

    原 handlers._collect_stickers —— **逐字搬运**。
    开关为 `sticker` 功能且 `config.sticker_collect` 才采集。
    """
    from ..stickers import get_sticker_store

    if not feature_enabled(config, event, "sticker") or not config.sticker_collect:
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


__all__ = [
    "feature_enabled",
    "affection_scope",
    "collect_stickers",
    "with_affection_footnote",
    "maybe_sync_affection_title",
    "finalize_affection_gain",
]


# ================================================================
# 好感度收尾（checkin / shop 命令 与 legacy 编排共用）
# ================================================================


def with_affection_footnote(text: str, result: Any, *, always_show: bool = True) -> str:
    """给回复追加好感度变化脚注。

    原 handlers._with_affection_footnote —— **逐字搬运**。
    """
    from ..affection import format_gain_footnote

    foot = format_gain_footnote(result, always_show=always_show)
    if not foot:
        return text
    return f"{text}\n{foot}"


async def maybe_sync_affection_title(
    bot: Bot,
    *,
    group_id: int,
    user_id: int,
    result: Any,
    config: PluginConfig,
) -> str:
    """好感度升级时同步群头衔（失败则返回提示文案）。

    原 handlers._maybe_sync_affection_title —— **逐字搬运**。
    需要 `config.affection_sync_special_title` 开启且机器人有管理权限。
    """
    from ..affection import all_affection_tier_titles
    from ..group_card import (
        apply_group_title,
        bot_can_manage_cards,
        title_apply_failure_note,
        title_apply_success_note,
    )

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


async def finalize_affection_gain(
    bot: Bot,
    *,
    scope: str,
    user_id: int,
    text: str,
    result: Any,
    config: PluginConfig,
    always_show: bool = True,
    group_id: int | None = None,
) -> str:
    """好感度变化后的回复收尾（脚注 + 可能的头衔同步）。

    原 handlers._finalize_affection_gain —— **逐字搬运**。
    """
    from ..affection import is_private_affection_scope

    text = with_affection_footnote(text, result, always_show=always_show)
    if group_id is not None and not is_private_affection_scope(scope):
        text += await maybe_sync_affection_title(
            bot,
            group_id=group_id,
            user_id=user_id,
            result=result,
            config=config,
        )
    return text
