"""平台层：OneBot 平台能力的共享实现。

Phase 6.6 第十六/十八节。这些 helper 原本定义在 `handlers.py`，
同时被**命令处理器**（affection / checkin / shop / verify）与
**Legacy 编排**使用，导致 `legacy → handlers` 反向依赖。

抽到本模块后依赖方向变成：

    handlers ──┐
               ├──→ platform ──→ (nonebot / config / services)
    legacy  ───┘

**为什么放在 gateway 层而不是新建 platform/ 目录**：
第十八节说"以现有仓库结构为准，不要为了提示词强行创造目录"。
`gateway/` 已经是"平台事件翻译层"，这些函数正是同一层的东西
（解析事件、发消息、解析会话），放这里语义一致。

**为什么用普通函数而不是 Service class**：
第十九节要求"如果现有 helper 很简单，不要为了一个函数创建 5 层 interface"。
这些都是无状态纯操作（或只依赖 bot/event/config），
包一层 class 只会增加间接层而没有收益。
"""

from __future__ import annotations

from typing import Any

from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)

from ..config import PluginConfig
from ..memory import session_id_for_group, session_id_for_private


# ================================================================
# 消息解析
# ================================================================


def plain_text(event: MessageEvent) -> str:
    """取事件的纯文本（去空白）。

    原 handlers._plain_text。被 sticker 采集与 legacy 编排共用。
    """
    return event.get_plaintext().strip()


def sender_name(event: MessageEvent) -> str:
    """取发送者显示名。

    原 handlers._sender_name —— **逐字搬运**：
    群消息用「群名片 → 昵称 → QQ 号」，私聊用「昵称 → QQ 号」。
    """
    if isinstance(event, GroupMessageEvent):
        info = event.sender
        return (
            (info.card if info and info.card else None)
            or (info.nickname if info else None)
            or str(event.user_id)
        )
    info = event.sender
    return (info.nickname if info and info.nickname else None) or str(event.user_id)


def resolve_session(
    event: MessageEvent, config: PluginConfig
) -> tuple[str, bool] | None:
    """解析会话 id。

    原 handlers._resolve_session —— **逐字搬运**。

    注意第二个返回值是 `is_group`（群消息 True），不是 `private_chat`：
        (session_id, True)  群
        (session_id, False) 私聊
    调用方按 `is_group` 使用，不要误读。

    群消息还受 `config.is_group_allowed()` 与 `config.enable_private` 约束。
    """
    if isinstance(event, GroupMessageEvent):
        if not config.is_group_allowed(event.group_id):
            return None
        return session_id_for_group(event.group_id), True
    if isinstance(event, PrivateMessageEvent):
        if not config.enable_private:
            return None
        return session_id_for_private(event.user_id), False
    return None


def is_to_me(event: MessageEvent) -> bool:
    """该消息是否明确指向机器人（@ 或私聊）。

    原 handlers._is_to_me —— **逐字等价**：
        return event.is_tome()

    被 legacy 编排使用；放在平台层因为它纯读事件结构，
    无 legacy 状态依赖。
    """
    return event.is_tome()


# ================================================================
# 发送
# ================================================================


async def reply(
    bot: Bot,
    event: MessageEvent,
    text: str,
    *,
    sticker: MessageSegment | None = None,
    voice: MessageSegment | None = None,
    image: MessageSegment | None = None,
) -> None:
    """发送回复（文本 + 可选媒体）。

    原 handlers._reply —— **逐字搬运**，行为完全保留：
    委托给 `send_text_reply`，分片 / 拟人延迟 / 多气泡 /
    媒体附带的全部参数都来自 config，不走本函数自己拼 MessageSegment。

    **注意**：这里刻意不自己组装 segments —— 那会绕过
    reply_send 的超长转发、拟人延迟、气泡切分等既有行为。
    """
    from ..config import get_config
    from ..persona import get_senpai_name
    from ..reply_send import send_text_reply

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
        forward_min_chars=config.forward_min_chars,
        forward_node_chars=config.forward_node_chars,
        nickname=get_senpai_name(),
        humanize_send=bool(config.humanize_send),
        typing_delay_ms_min=config.typing_delay_ms_min,
        typing_delay_ms_max=config.typing_delay_ms_max,
        bubble_max=config.bubble_max,
        bubble_min_chars=config.bubble_min_chars,
        bubble_prefer_chars=config.bubble_prefer_chars,
        bubble_total_max_chars=config.bubble_total_max_chars,
    )


__all__ = [
    "plain_text",
    "sender_name",
    "resolve_session",
    "is_to_me",
    "reply",
]
