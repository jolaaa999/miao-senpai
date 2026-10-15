"""把「合并转发」的聊天记录渲染成模型能读的文本。

为什么单独成模块：与 attachments.py 同源——取转发内容要调 OneBot 的
get_forward_msg（网络 IO），既不属于 handler，也不该塞进同步的桥接装配函数。
桥接层在异步的 handle_agent_message 里先解析好，再当作字符串传进去。

安全与配额边界（沿用 attachments.py 的取舍）：
    - 转发 id 只能来自消息段，模型无法构造任意 id
    - 最多渲染 MAX_NODES 条、总计 MAX_CHARS 字符，避免一次吃爆上下文
    - 取不到内容返回空串而非抛异常：读不了就说读不了，不能拦掉回复
"""

from __future__ import annotations

import json
from typing import Any

# 单次转发最多渲染多少条消息。群里常见十几条，但有人会转几百条的长记录；
# 超出的部分用一句省略号标出来，不静默丢弃——否则模型会以为记录就这么多。
MAX_NODES = 40

# 渲染文本总字数上限。与 attachments.MAX_TEXT_CHARS 同一量级：
# 都是「一次调用能安全喂给模型」的预算。
MAX_CHARS = 4000

# 单条消息的文本上限。一条就顶满整个预算的极端情况要挡住。
MAX_NODE_CHARS = 300

# 段类型 → 占位符。转发记录里非文本段很常见（图片、表情、语音）。
# 渲染成占位符而不是直接丢掉：模型才知道「这里本来有张图」，
# 而不是把前后两句话当成连续的上下文。
_SEGMENT_PLACEHOLDER = {
    "image": "[图片]",
    "face": "[表情]",
    "mface": "[表情]",
    "bface": "[表情]",
    "sface": "[表情]",
    "video": "[视频]",
    "record": "[语音]",
    "file": "[文件]",
    "forward": "[转发的聊天记录]",
    "json": "[卡片]",
    "xml": "[卡片]",
    "poke": "[戳一戳]",
}

# 取转发记录 id 时要试的字段名。OneBot 标准写 message_id，
# NapCat 实际给的是 id；另外两种是别处见过的写法。
_ID_KEYS = ("id", "message_id", "res_id", "forward_id")

# 合并转发在部分实现里不走 forward 段，而是发一个 json 卡片，
# 载荷里写着 com.tencent.multimsg，真正的记录 id 藏在 resid。
# 两种形态都得认：只认 forward 的话，线上一旦是卡片形态，
# 学姐拿到的就是一个没内容的占位符，功能等于没做。
_MULTIMSG_HINT = "multimsg"


def _find_key(obj: Any, key: str) -> str:
    """在嵌套的 dict/list 里找第一个名为 key 的非空标量值。

    卡片载荷的层级随 QQ 版本变（meta.detail.resid / meta.resid / 更深的
    嵌套），写死路径迟早会失效，所以递归找。
    """
    if isinstance(obj, dict):
        value = obj.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
        for item in obj.values():
            found = _find_key(item, key)
            if found:
                return found
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            found = _find_key(item, key)
            if found:
                return found
    return ""


def _multimsg_id(data: dict[str, Any]) -> str:
    """从 json 卡片段里抠出合并转发的记录 id；不是卡片则返回空串。

    只认带 multimsg 标记的载荷，避免把普通的分享卡片（音乐、链接、
    小程序）误当成聊天记录去拉取。
    """
    raw = data.get("data") or data.get("json") or ""
    parsed: Any = None
    if isinstance(raw, str):
        if _MULTIMSG_HINT not in raw:
            return ""
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return ""
    elif isinstance(raw, dict):
        try:
            dumped = json.dumps(raw, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""
        if _MULTIMSG_HINT not in dumped:
            return ""
        parsed = raw
    else:
        return ""

    # resid 是记录 id；uniseq 是退路（个别版本只给这个）
    return _find_key(parsed, "resid") or _find_key(parsed, "uniseq")


def extract_forward_segments(message: Any) -> list[dict[str, Any]]:
    """从 OneBot 消息里取出合并转发的段（按出现顺序）。

    两种投递形态都认：
        1. type=forward，data 里直接带 id（OneBot 标准）
        2. type=json，data 是 com.tencent.multimsg 卡片，id 藏在 resid
    统一归一化成带 id 的 dict，调用方不用关心来源形态。
    与 attachments.extract_file_segments 同构：只认段类型，不碰记录内容。
    """
    found: list[dict[str, Any]] = []
    if message is None:
        return found
    for seg in message:
        seg_type = getattr(seg, "type", None)
        data = dict(getattr(seg, "data", {}) or {})
        if not data:
            continue
        if seg_type == "forward":
            found.append(data)
            continue
        if seg_type == "json":
            forward_id = _multimsg_id(data)
            if forward_id:
                found.append({"id": forward_id})
    return found


def forward_id_of(data: dict[str, Any]) -> str:
    """取转发记录 id。字段名各实现不一致，按 _ID_KEYS 逐个试。"""
    for key in _ID_KEYS:
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


def _seg_get(seg: Any, key: str, default: Any = None) -> Any:
    """兼容两种段表示：NoneBot 的 Seg 对象 与 原始 dict。

    转发节点里的 content 直接来自 API 返回值，往往是 dict；
    而外层消息段是 NoneBot 解析过的对象。同一份代码要同时吃两种。
    """
    value = getattr(seg, key, None)
    if value is None and isinstance(seg, dict):
        value = seg.get(key)
    return default if value is None else value


def render_node_content(content: Any) -> str:
    """把一条转发消息的内容渲染成一行文本。

    转发记录里每条消息的 content 又是一个消息段数组（与普通消息同构），
    所以必须再走一遍段解析；纯字符串也接受（部分实现直接给文本）。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        text = content.strip()
    else:
        parts: list[str] = []
        for seg in content:
            seg_type = str(_seg_get(seg, "type", "") or "")
            data = _seg_get(seg, "data", {}) or {}
            if not isinstance(data, dict):
                data = {}
            if seg_type == "text":
                parts.append(str(data.get("text") or ""))
            elif seg_type == "at":
                target = str(data.get("name") or data.get("qq") or "").strip()
                parts.append("@" + (target or "某人"))
            else:
                # 认不出的段类型给空串：宁可不显示，也不要吐出原始 CQ 码
                parts.append(_SEGMENT_PLACEHOLDER.get(seg_type, ""))
        text = "".join(parts).strip()

    if len(text) > MAX_NODE_CHARS:
        text = text[: MAX_NODE_CHARS - 1].rstrip() + "…"
    return text


def _iter_nodes(payload: Any) -> list[Any]:
    """从 get_forward_msg 的返回里取节点数组。

    返回形状在实现/版本间有差异（dict 或对象、messages 或 message 或 nodes），
    统一在这里抹平，后面的逻辑就只面对 list。
    """
    if payload is None:
        return []
    data = payload
    if not isinstance(data, dict):
        data = getattr(data, "__dict__", None) or {}
    messages = (
        data.get("messages")
        or data.get("message")
        or data.get("nodes")
        or []
    )
    if isinstance(messages, dict):
        messages = [messages]
    if isinstance(messages, (list, tuple)):
        return list(messages)
    return []


def _node_sender(node: Any) -> str:
    """取该条转发的发送者显示名。

    优先昵称（记录里展示的就是群名片/昵称），没有就退到 QQ 号。
    """
    sender = _seg_get(node, "sender", None)
    if isinstance(sender, dict):
        name = sender.get("nickname") or sender.get("card") or sender.get("user_id")
    else:
        name = (
            _seg_get(node, "nickname", None)
            or _seg_get(node, "card", None)
            or _seg_get(node, "user_id", None)
        )
    return str(name or "").strip()


def _node_content(node: Any) -> Any:
    """取该条转发的内容；部分实现把内容放在 message 键下。"""
    content = _seg_get(node, "content", None)
    if content is None:
        content = _seg_get(node, "message", None)
    return content


def render_forward(nodes: list[Any], *, sender_hint: str = "") -> str:
    """把节点数组渲染成模型可读的聊天记录文本。

    Args:
        sender_hint: 转发者本人的名字。整条记录一条发送者都没有时，
            用它兜底，免得渲染出一堆没头没尾的句子。
    """
    if not nodes:
        return ""

    lines: list[str] = []
    for node in nodes[:MAX_NODES]:
        sender = _node_sender(node) or sender_hint
        body = render_node_content(_node_content(node))
        if not body:
            # 只有图片/表情的一条也要保留，否则记录会断档
            if not sender:
                continue
            body = ""
        lines.append((sender + "：" if sender else "") + body)

    if len(nodes) > MAX_NODES:
        lines.append("…（记录里还有 %d 条没显示）" % (len(nodes) - MAX_NODES))

    text = "\n".join(lines).strip()
    if len(text) > MAX_CHARS:
        text = text[: MAX_CHARS - 1].rstrip() + "…"
    return text


async def _fetch_nodes(bot: Any, forward_id: str) -> list[Any]:
    """问 OneBot 要转发记录的节点。

    参数名各实现不同（标准用 message_id，NapCat 用 id），逐个试；
    全部失败返回空表，由调用方决定怎么降级。
    """
    for kwargs in ({"id": forward_id}, {"message_id": forward_id}):
        try:
            payload = await bot.call_api("get_forward_msg", **kwargs)
        except Exception:  # noqa: BLE001 — 换下一种参数名继续试
            continue
        nodes = _iter_nodes(payload)
        if nodes:
            return nodes
    return []


async def resolve_forward_text(
    bot: Any,
    segments: list[dict[str, Any]],
    *,
    sender_hint: str = "",
) -> str:
    """取出一条或多条转发的聊天记录并渲染成文本；取不到返回空串。

    为什么主动取而不是等模型调工具：用户在群里转一段聊天记录，
    内容才是他想让学姐看的东西。只把 CQ 码给模型，她只能瞎猜——
    和「发了文档就该去读」是同一个道理。
    """
    if bot is None or not segments:
        return ""

    blocks: list[str] = []
    for data in segments:
        forward_id = forward_id_of(data)
        if not forward_id:
            continue
        nodes = await _fetch_nodes(bot, forward_id)
        rendered = render_forward(nodes, sender_hint=sender_hint)
        if rendered:
            blocks.append(rendered)
    return "\n\n".join(blocks).strip()


__all__ = [
    "MAX_CHARS",
    "MAX_NODES",
    "MAX_NODE_CHARS",
    "extract_forward_segments",
    "forward_id_of",
    "render_forward",
    "render_node_content",
    "resolve_forward_text",
]
