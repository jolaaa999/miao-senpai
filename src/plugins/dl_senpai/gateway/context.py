"""归一化消息上下文：把 OneBot 事件翻译成平台无关的形状。

设计动机（Phase 3 任务 A）：重构前 `handlers.py` 的 7 个 matcher 直接把
`GroupMessageEvent` / `PrivateMessageEvent` / `GroupIncreaseNoticeEvent`
透传给所有业务模块，导致 47 个模块里有 30+ 处 `isinstance(event,
GroupMessageEvent)` 分支、20+ 处 `event.group_id` / `event.user_id` /
`event.message_id` 直接取值。结果是**业务逻辑与 OneBot 协议强绑定**，
换一个适配器（比如 Telegram）要改遍所有模块。

本模块把「路由与分发需要的那部分信息」提出来：

    MessageContext:
        platform / message_id / user_id / group_id / session_id
        message_type / raw_message / plain_text
        is_to_me / is_private / reply_to
        actor / trace_id
        raw_event   ← 过渡期保留

关于 `raw_event`：**保留是刻意的**。现有业务大量依赖 OneBot 特有字段
（`event.message` 消息段、`event.sender.card` 名片、`event.reply` 引用），
一次性切断会让 Phase 3 变成大爆炸重写。约定是：

    Router / Command / Plugin  ——  只允许读归一化字段
    Legacy Bridge 内部          ——  允许读 raw_event（Phase 4+ 再收敛）

即"新代码不碰 raw_event，老代码暂不动"。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

# 消息类型
MessageType = Literal["group", "private", "notice", "unknown"]

# session id 前缀常量，避免各处硬编码字符串
SESSION_GROUP = "group"
SESSION_PRIVATE = "private"

# 事件类型名（与 EventTypes 的命名保持一致）
NOTICE_GROUP_INCREASE = "notice.group_increase"
NOTICE_GROUP_DECREASE = "notice.group_decrease"
NOTICE_OTHER = "notice.other"
# 撤回通知：群（group_recall）/ 好友（friend_recall）。用户撤自己消息、管理员撤别人、
# 以及「她发的消息被别人撤了」走的是同一类事件，靠 author/operator 区分。
NOTICE_GROUP_RECALL = "notice.group_recall"
NOTICE_FRIEND_RECALL = "notice.friend_recall"


@dataclass
class MessageContext:
    """一次消息/通知的归一化上下文。

    字段命名对齐 Phase 3 规范；`session_id` 沿用现有 ChatMemory 的
    `group:{gid}` / `private:{uid}` 格式，保证记忆存储零迁移。
    """

    platform: str = "onebot.v11"
    message_id: str = ""

    user_id: str = ""
    group_id: str = ""
    self_id: str = ""

    # 会话标识（= 现有 ChatMemory scope，零迁移）
    session_id: str = ""

    message_type: MessageType = "unknown"
    # 原始消息段（OneBot Message 对象），供 legacy 使用
    raw_message: Any = None
    # 去掉 CQ 码/富文本后的纯文本
    plain_text: str = ""

    is_to_me: bool = False
    is_private: bool = False

    # 被引用消息的 id（OneBot 的 event.reply）
    reply_to: str = ""

    # 发送者显示名（优先群名片）
    actor: str = ""

    trace_id: str = ""

    # 通知类事件的子类型（group_increase / group_decrease / group_recall / ...）
    notice_type: str = ""

    # 撤回通知的细节（只有 notice_type 是 recall 时才有值）
    # recalled_message_id：被撤回那条消息的 id
    # recall_author_id：被撤回消息的**作者**（等于 self_id 就是她被撤了）
    # recall_operator_id：**执行撤回的人**（管理员撤别人时与 author 不同）
    recalled_message_id: str = ""
    recall_author_id: str = ""
    recall_operator_id: str = ""

    # 过渡期逃生舱：legacy bridge 需要 OneBot 私有能力时使用
    raw_event: Any = None
    bot: Any = None
    # 路由中间结果（阶段间传递）
    extra: dict[str, Any] = field(default_factory=dict)

    # -- 便捷判定 --

    @property
    def is_group(self) -> bool:
        return self.message_type == "group"

    @property
    def is_notice(self) -> bool:
        return self.message_type == "notice"

    def has_images(self) -> bool:
        """是否含图片类消息段。需要 raw_event；无则返回 False。"""
        try:
            return any(seg.type in {"image", "mface"} for seg in self.raw_event.message)
        except Exception:  # noqa: BLE001
            return False

    def get(self, key: str, default: Any = None) -> Any:
        return self.extra.get(key, default)

    def as_log_fields(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "session_id": self.session_id,
            "message_type": self.message_type,
        }
        if self.message_id:
            out["message_id"] = self.message_id
        if self.group_id:
            out["group_id"] = self.group_id
        if self.user_id:
            out["user_id"] = self.user_id
        if self.trace_id:
            out["trace_id"] = self.trace_id
        return out


# -- session id --

def session_id_for_group(group_id: int | str) -> str:
    """群 session：`group:{gid}`。

    与 `dl_senpai.memory.session_id_for_group` **完全一致**——这是刻意的，
    保证 ChatMemory 存储路径不变、生产数据零迁移。
    """
    return f"{SESSION_GROUP}:{group_id}"


def session_id_for_private(user_id: int | str) -> str:
    """私聊 session：`private:{uid}`。同上，与现有 memory 一致。"""
    return f"{SESSION_PRIVATE}:{user_id}"


# -- Agent 并发键 --

def agent_session_key(ctx: "MessageContext") -> str:
    """Agent 阶段的**并发隔离键**。

    关键区别（Phase 3 任务 F）：这里**不是** `session_id`。

    - `session_id`（`group:{gid}`）是**记忆**作用域：一个群共用一段上下文。
    - `agent_session_key`（`group:{gid}:user:{uid}`）是**并发**作用域。

    为什么必须区分：如果一个群 100 人共用 `group:{gid}` 做串行队列，
    任何一个人触发一次 5 秒的 LLM 调用，其余 99 人全部排队等待——
    群聊体验直接崩掉。按「群+人」隔离后，同一个人连发消息仍严格有序
    （避免上下文错乱），不同人之间完全并发。

    私聊天然是一对一，用 `private:{uid}` 即可。
    """
    if ctx.group_id:
        return f"{SESSION_GROUP}:{ctx.group_id}:user:{ctx.user_id}"
    return f"{SESSION_PRIVATE}:{ctx.user_id}"


__all__ = [
    "MessageContext",
    "MessageType",
    "session_id_for_group",
    "session_id_for_private",
    "agent_session_key",
    "SESSION_GROUP",
    "SESSION_PRIVATE",
    "NOTICE_GROUP_INCREASE",
    "NOTICE_GROUP_DECREASE",
    "NOTICE_OTHER",
]
