"""OneBot 适配器：原始 NoneBot 事件 → `MessageContext`。

这一层是 Gateway，职责**只有一个**：翻译。它不做路由决策、不做权限判断、
不调业务代码。这样做的价值是让"消息从哪来"和"消息该谁处理"彻底解耦。
"""

from __future__ import annotations

from typing import Any

from ..observability.context import current_trace, new_trace_id
from .context import (
    NOTICE_FRIEND_RECALL,
    NOTICE_GROUP_DECREASE,
    NOTICE_GROUP_INCREASE,
    NOTICE_GROUP_RECALL,
    NOTICE_OTHER,
    MessageContext,
    session_id_for_group,
    session_id_for_private,
)

# 惰性 import：单测环境可能没有 nonebot adapter
try:
    from nonebot.adapters.onebot.v11 import (
        GroupDecreaseNoticeEvent,
        GroupIncreaseNoticeEvent,
        GroupMessageEvent,
        MessageEvent,
        NoticeEvent,
        PrivateMessageEvent,
    )
except Exception:  # noqa: BLE001
    GroupMessageEvent = PrivateMessageEvent = MessageEvent = None  # type: ignore[assignment,misc]
    NoticeEvent = GroupIncreaseNoticeEvent = GroupDecreaseNoticeEvent = None  # type: ignore[assignment,misc]


def _sender_display(event: Any) -> str:
    """取发送者显示名：优先群名片，其次昵称，兜底 QQ 号。

    与 `handlers._sender_name()` 的取值顺序保持一致，避免同一用户在
    路由日志与业务日志里显示成两个不同的名字。
    """
    user_id = getattr(event, "user_id", "")
    info = getattr(event, "sender", None)
    if info is not None:
        card = getattr(info, "card", None)
        if card:
            return str(card)
        nickname = getattr(info, "nickname", None)
        if nickname:
            return str(nickname)
    return str(user_id)


def _render_segments(message: Any, self_id: str = "") -> str | None:
    """按消息段渲染成「模型能读的一句话」，拿不到消息段时返回 None。

    和 `event.get_plaintext()` 只差一处，但很要命：OneBot v11 的
    `extract_plain_text()` 只拼 `is_text()` 的段，at 段被整段丢掉——于是
    「@学姐 你看看@学生 她说的内容」到模型手里只剩「 你看看  她说的内容」，
    被 @ 的人彻底消失，她只能瞎猜「她」指谁。
    这里把 at 段渲染成 `@名字`（NapCat 一般带 name，没有就退 QQ 号：宁可显示
    数字，也别让指代消失）；@ 自己的那一段跳过（她本来就知道这条冲她来的）。
    其它非文本段维持旧行为：不参与文本（图片/表情另走 vision 通路，避免重复）。
    """
    try:
        segments = list(message)
    except Exception:  # noqa: BLE001 — 不是消息段列表就交给调用方回退
        return None
    if not segments:
        return None
    parts: list[str] = []
    for seg in segments:
        seg_type = str(getattr(seg, "type", "") or "")
        data = dict(getattr(seg, "data", {}) or {})
        if seg_type == "text":
            parts.append(str(data.get("text") or ""))
        elif seg_type == "at":
            qq = str(data.get("qq") or "").strip()
            if qq == "all":
                parts.append("@全体成员")
            elif not self_id or qq != self_id:
                name = str(data.get("name") or "").strip()
                parts.append("@" + (name or qq or "某人"))
    return "".join(parts).strip()


def _plain_text(event: Any, self_id: str = "") -> str:
    """取给模型看的纯文本：正文 + @ 对象，不含 CQ 码。

    优先自己渲染消息段（见 `_render_segments` 里为什么不能用 get_plaintext）；
    拿不到消息段时才回退到 NoneBot 的纯文本实现。
    """
    rendered = _render_segments(getattr(event, "message", None), self_id)
    if rendered is not None:
        return rendered
    try:
        return event.get_plaintext() or ""
    except Exception:  # noqa: BLE001
        return ""


def _has_media(event: Any) -> bool:
    """消息里有没有图片/表情/语音/转发这类"内容本身不是文字"的段。

    为什么要问：只 @ 一句不写字、和只发一张图，plain_text 都是空串。
    前者要补占位让她知道有人在叫她；后者要走视觉通路，不该被写成"没说别的"。
    """
    try:
        segments = list(getattr(event, "message", None) or [])
    except Exception:  # noqa: BLE001
        return False
    media = {"image", "mface", "record", "video", "file", "forward", "node"}
    return any(str(getattr(seg, "type", "") or "") in media for seg in segments)


def _at_only_placeholder() -> str:
    """只 @ 不说话时的占位文本。

    以前这种情况 plain_text 是空串，下游把它当成"没内容"整条丢掉，
    表现就是「@她 一点反应都没有」。占位语句让她明白：被叫了，但对方没给内容，
    于是可以自己接一句（应一声、问什么事），而不是沉默。
    """
    return "（只@了你，没打字）"


def _is_tome(event: Any, bot: Any) -> bool:
    """是否 @ 了机器人。

    优先用 adapter 的 `is_tome()`（它会正确处理 @全体成员 等边界）；
    失败时回退到手工扫描 at 段，与 `handlers._message_ats_bot` 逻辑一致。
    """
    try:
        if event.is_tome():
            return True
    except Exception:  # noqa: BLE001
        pass
    self_id = str(getattr(bot, "self_id", "") or getattr(event, "self_id", "") or "")
    try:
        for seg in event.message:
            if seg.type == "at" and str(seg.data.get("qq") or "") == self_id:
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _reply_id(event: Any) -> str:
    reply = getattr(event, "reply", None)
    if reply is None:
        return ""
    mid = getattr(reply, "message_id", None)
    return str(mid) if mid is not None else ""


def from_onebot_event(event: Any, bot: Any = None) -> MessageContext | None:
    """把任意 NoneBot 事件转成 `MessageContext`。

    无法识别的类型返回 None（由调用方决定是否忽略），而不是抛异常——
    群里会出现大量无关通知事件，让它们变成异常会污染日志。
    """
    if event is None:
        return None

    # 继承当前 trace（如果有），否则新建——保证每条消息都能被单独追踪
    ctx_trace = current_trace()
    trace = ctx_trace.trace_id if ctx_trace is not None else new_trace_id()

    base: dict[str, Any] = {
        "platform": "onebot.v11",
        "message_id": str(getattr(event, "message_id", "") or ""),
        "user_id": str(getattr(event, "user_id", "") or ""),
        "group_id": str(getattr(event, "group_id", "") or ""),
        "self_id": str(
            getattr(bot, "self_id", "") or getattr(event, "self_id", "") or ""
        ),
        "actor": _sender_display(event),
        "raw_message": getattr(event, "message", None),
        "raw_event": event,
        "bot": bot,
        "reply_to": _reply_id(event),
        "trace_id": trace,
    }

    if GroupMessageEvent is not None and isinstance(event, GroupMessageEvent):
        to_me = _is_tome(event, bot)
        plain = _plain_text(event, base["self_id"])
        if not plain and to_me and not _has_media(event):
            plain = _at_only_placeholder()
        return MessageContext(
            **base,
            message_type="group",
            session_id=session_id_for_group(event.group_id),
            plain_text=plain,
            is_to_me=to_me,
            is_private=False,
        )

    if PrivateMessageEvent is not None and isinstance(event, PrivateMessageEvent):
        # 私聊天然是"找我"，空文本同样会被丢掉，所以只发图/只发表情之外的空消息
        # 也给一句占位（群聊那条同样的坑见上面的 _at_only_placeholder）。
        plain = _plain_text(event, base["self_id"])
        if not plain and not _has_media(event):
            plain = _at_only_placeholder()
        return MessageContext(
            **base,
            message_type="private",
            session_id=session_id_for_private(event.user_id),
            plain_text=plain,
            # 私聊天然是"找我"，与 handlers.handle_private(force_mention=True) 一致
            is_to_me=True,
            is_private=True,
        )

    if NoticeEvent is not None and isinstance(event, NoticeEvent):
        notice_type = NOTICE_OTHER
        if GroupIncreaseNoticeEvent is not None and isinstance(
            event, GroupIncreaseNoticeEvent
        ):
            notice_type = NOTICE_GROUP_INCREASE
        elif GroupDecreaseNoticeEvent is not None and isinstance(
            event, GroupDecreaseNoticeEvent
        ):
            notice_type = NOTICE_GROUP_DECREASE

        # 撤回单独认一遍：这里按 notice_type 字符串判断而不是 isinstance，
        # 因为 nonebot 的撤回事件类在不同版本里名字不同（GroupRecallNoticeEvent /
        # FriendRecallNoticeEvent），而 OneBot 的 notice_type 字段是稳定的。
        raw_notice = str(getattr(event, "notice_type", "") or "")
        if raw_notice == "group_recall":
            notice_type = NOTICE_GROUP_RECALL
        elif raw_notice == "friend_recall":
            notice_type = NOTICE_FRIEND_RECALL

        gid = str(getattr(event, "group_id", "") or "")
        # 撤回事件里 user_id 是「被撤回消息的作者」，operator_id 是「执行撤回的人」。
        # 两者相等=本人自撤；不等=管理员/别人撤的。她要不要开口全靠这个区分。
        is_recall = notice_type in (NOTICE_GROUP_RECALL, NOTICE_FRIEND_RECALL)
        recall_author = str(getattr(event, "user_id", "") or "") if is_recall else ""
        return MessageContext(
            **base,
            message_type="notice",
            # 通知没有"会话"语义，但给个稳定 key 方便日志聚合
            session_id=(
                session_id_for_group(gid)
                if gid
                else (
                    session_id_for_private(recall_author)
                    if notice_type == NOTICE_FRIEND_RECALL and recall_author
                    else ""
                )
            ),
            plain_text="",
            is_to_me=False,
            is_private=notice_type == NOTICE_FRIEND_RECALL,
            notice_type=notice_type,
            recalled_message_id=(
                str(getattr(event, "message_id", "") or "") if is_recall else ""
            ),
            recall_author_id=recall_author,
            recall_operator_id=(
                str(getattr(event, "operator_id", "") or "") or recall_author
            ) if is_recall else "",
        )

    # 兜底：未知消息类事件（例如 MessageEvent 的其它子类）
    if MessageEvent is not None and isinstance(event, MessageEvent):
        return MessageContext(
            **base,
            message_type="unknown",
            session_id="",
            plain_text=_plain_text(event, base["self_id"]),
            is_to_me=_is_tome(event, bot),
            is_private=False,
        )

    return None


__all__ = ["from_onebot_event", "from_onebot_event_with_asr"]


# at 段在 OneBot v11 规范里只有 qq，name 是实现私有扩展（NapCat 不一定给）。
# 拿不到 name 时她看到的是「@123456789」，等于没看见是谁；所以缺 name 的 at
# 用群名片补一次，按 (群号, QQ) 缓存一小时——同一个人在同一群里反复 @ 不该反复查。
_AT_NAME_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
_AT_NAME_TTL = 3600.0


async def _fill_at_names(
    text: str, event: Any, bot: Any, self_id: str = ""
) -> str:
    """把文本里的 `@QQ号` 换成 `@群名片/昵称`；查不到就保留号码、绝不抛错。

    为什么在文本层替换而不是重渲染消息段：`plain_text` 可能来自 get_plaintext 的
    回退路径（无消息段），此时整体重渲染会把正文丢掉；按号码替换最稳。
    """
    import time as _time

    if not text or "@" not in text or bot is None:
        return text
    group_id = str(getattr(event, "group_id", "") or "")
    if not group_id:
        return text  # 私聊没有群名片可查
    try:
        segments = list(getattr(event, "message", None) or [])
    except Exception:  # noqa: BLE001
        return text
    pending: list[str] = []
    for seg in segments:
        if str(getattr(seg, "type", "") or "") != "at":
            continue
        data = dict(getattr(seg, "data", {}) or {})
        qq = str(data.get("qq") or "").strip()
        if not qq or qq == "all" or qq == self_id or data.get("name"):
            continue  # 已有名字 / 全体 / @自己 都不用查
        if qq in pending:
            continue
        pending.append(qq)
    for qq in pending:
        key = (group_id, qq)
        hit = _AT_NAME_CACHE.get(key)
        name = ""
        if hit and _time.time() - hit[1] < _AT_NAME_TTL:
            name = hit[0]
        else:
            try:
                info = await bot.get_group_member_info(
                    group_id=int(group_id), user_id=int(qq), no_cache=False
                )
                name = str((info or {}).get("card") or (info or {}).get("nickname") or "").strip()
            except Exception:  # noqa: BLE001 — 查不到名字不影响这条消息
                name = ""
            if name:
                _AT_NAME_CACHE[key] = (name, _time.time())
        if name:
            text = text.replace(f"@{qq}", f"@{name}")
    return text

async def from_onebot_event_with_asr(event: Any, bot: Any = None) -> MessageContext | None:
    """归一化事件，顺带把语音条转成文字。

    为什么单开一个异步包装而不是把 from_onebot_event 改成 async：
    那是二十多个测试和 legacy 路径共用的纯函数，改签名会牵动一大片。
    这里只在「真正接消息的那一个入口」加一层，把转写结果写回 plain_text，
    下游（记忆/触发/模型）完全不用知道自己拿到的是语音还是打字。
    """
    ctx = from_onebot_event(event, bot)
    if ctx is None or bot is None:
        return ctx
    # 先把 @QQ号 补成 @昵称，再做语音转写追加：两步都只改文本，顺序不影响结果，
    # 但放在最前面能保证下游（记忆/模型）拿到的永远是补全后的版本。
    try:
        ctx.plain_text = await _fill_at_names(ctx.plain_text, event, bot, ctx.self_id)
    except Exception:  # noqa: BLE001 — 补名字失败必须静默退化
        pass
    try:
        from ..asr import transcribe_event

        text = await transcribe_event(event, bot)
    except Exception:  # noqa: BLE001 — 转写失败必须静默退化
        return ctx
    if not text:
        return ctx
    # 保留「这是语音说的」这个事实：她会据此决定要不要也用语音回
    spoken = f"（语音）{text}"
    existing = (ctx.plain_text or "").strip()
    ctx.plain_text = f"{existing}\n{spoken}".strip() if existing else spoken
    return ctx
