"""自动同意好友申请与拉群邀请；入群申请（需群管审核）一律不自动同意。"""

from __future__ import annotations

from nonebot import logger, on_request
from nonebot.adapters.onebot.v11 import Bot, FriendRequestEvent, GroupRequestEvent, RequestEvent

from .config import get_config

request_matcher = on_request(priority=3, block=True)


@request_matcher.handle()
async def handle_social_request(bot: Bot, event: RequestEvent) -> None:
    config = get_config()
    if isinstance(event, FriendRequestEvent):
        if not config.auto_accept_friend:
            return
        try:
            remark = (config.auto_accept_friend_remark or "").strip()
            await event.approve(bot, remark=remark)
            logger.info(
                f"dl_senpai accepted friend request user={event.user_id} "
                f"comment={(event.comment or '')[:48]!r}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"dl_senpai friend approve failed user={event.user_id}: {exc}"
            )
        return

    if isinstance(event, GroupRequestEvent):
        # sub_type=add：别人申请进群（仅群管会收到）。学姐是群管也不自动同意。
        if (event.sub_type or "").strip().lower() == "add":
            logger.info(
                f"dl_senpai skip group join request "
                f"group={event.group_id} user={event.user_id} "
                f"comment={(event.comment or '')[:48]!r}"
            )
            return
        # sub_type=invite：别人拉学姐进群 —— 仍可由配置自动同意
        if not config.auto_accept_group:
            return
        try:
            await event.approve(bot)
            logger.info(
                f"dl_senpai accepted group invite "
                f"group={event.group_id} user={event.user_id} "
                f"sub_type={event.sub_type} comment={(event.comment or '')[:48]!r}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"dl_senpai group invite approve failed "
                f"group={event.group_id} user={event.user_id}: {exc}"
            )
