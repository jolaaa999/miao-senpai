"""@ 对象进不进模型输入：网关渲染层的回归测试。

为什么单独测：OneBot 的 `extract_plain_text()` 只拼 text 段，at 段的 qq/name
会被整段丢弃。「@学姐 你看看@学生 她说的内容」到模型手里只剩「 你看看  她说的内容」，
指代的人直接消失。这类退化不会报错、只会让回答变傻，必须靠测试钉住。
"""

from __future__ import annotations

import pytest
from nonebot.adapters.onebot.v11 import Message, MessageSegment

from dl_senpai.gateway.onebot_adapter import (
    _AT_NAME_CACHE,
    _fill_at_names,
    _plain_text,
    _render_segments,
)

SELF_ID = "999"


class _FakeBot:
    def __init__(self, info: dict | None = None) -> None:
        self._info = info or {}
        self.calls: list[tuple[int, int]] = []

    async def get_group_member_info(self, *, group_id: int, user_id: int, no_cache: bool = False):
        self.calls.append((group_id, user_id))
        if user_id not in self._info:
            raise RuntimeError("member not found")
        return self._info[user_id]


class _FakeEvent:
    def __init__(self, message: Message, group_id: int = 100) -> None:
        self.message = message
        self.group_id = group_id

    def get_plaintext(self) -> str:
        return self.message.extract_plain_text()


def test_render_keeps_other_peoples_at() -> None:
    """别人被 @ 时，名字/号码必须留在文本里（旧实现的空档）。"""
    msg = Message([
        MessageSegment.at(SELF_ID),
        MessageSegment.text(" 你看看 "),
        MessageSegment.at("12345"),
        MessageSegment.text(" 她说的内容"),
    ])
    rendered = _render_segments(msg, SELF_ID)
    assert "@12345" in rendered
    assert "她说的内容" in rendered
    # @ 自己不占文本（她已经知道这条冲她来的）
    assert SELF_ID not in rendered


def test_render_prefers_at_name() -> None:
    """at 段自带 name 时直接用名字，不去查群名片。"""
    msg = Message([
        MessageSegment("at", {"qq": "12345", "name": "学生"}),
        MessageSegment.text(" 你好"),
    ])
    assert _render_segments(msg, SELF_ID) == "@学生 你好"


def test_render_at_all_kept_as_word() -> None:
    msg = Message([MessageSegment.at("all"), MessageSegment.text(" 开会了")])
    assert _render_segments(msg, SELF_ID) == "@全体成员 开会了"


def test_render_plain_text_unchanged() -> None:
    """纯文本消息必须与旧行为完全一致（不能因这次改动动了普通聊天）。"""
    msg = Message([MessageSegment.text("今天天气不错")])
    assert _render_segments(msg, SELF_ID) == "今天天气不错"


def test_render_returns_none_without_segments() -> None:
    """拿不到消息段时返回 None，交给调用方回退 get_plaintext。"""
    assert _render_segments(None, SELF_ID) is None
    assert _render_segments([], SELF_ID) is None


def test_plain_text_falls_back_when_message_missing() -> None:
    class _NoMessage:
        def get_plaintext(self) -> str:
            return "回退文本"

    assert _plain_text(_NoMessage(), SELF_ID) == "回退文本"


@pytest.mark.asyncio
async def test_fill_at_names_uses_group_card() -> None:
    """缺 name 时按群名片补；查到的名字缓存一小时，第二次不再请求。"""
    _AT_NAME_CACHE.clear()
    bot = _FakeBot({12345: {"card": "学生甲", "nickname": "小王"}})
    event = _FakeEvent(Message([MessageSegment.at("12345"), MessageSegment.text(" 在吗")]))
    text = _render_segments(event.message, SELF_ID)
    out = await _fill_at_names(text, event, bot, SELF_ID)
    assert out == "@学生甲 在吗"
    assert bot.calls == [(100, 12345)]
    # 第二次走缓存，不再打 NapCat
    out2 = await _fill_at_names(text, event, bot, SELF_ID)
    assert out2 == "@学生甲 在吗"
    assert bot.calls == [(100, 12345)]


@pytest.mark.asyncio
async def test_fill_at_names_failure_keeps_number() -> None:
    """查不到成员信息时保留 QQ 号，绝不抛错（否则整条回复会挂）。"""
    _AT_NAME_CACHE.clear()
    bot = _FakeBot({})
    event = _FakeEvent(Message([MessageSegment.at("54321"), MessageSegment.text(" 在吗")]))
    out = await _fill_at_names("@54321 在吗", event, bot, SELF_ID)
    assert out == "@54321 在吗"


@pytest.mark.asyncio
# -- 只 @ 不说话 --


def _group_event(message: Message, *, self_id: int = 999) -> object:
    """构造一条真实的 GroupMessageEvent（用于验证网关层的占位逻辑）。"""
    from nonebot.adapters.onebot.v11 import GroupMessageEvent

    return GroupMessageEvent(
        time=1,
        self_id=self_id,
        post_type="message",
        sub_type="normal",
        user_id=123,
        message_type="group",
        message_id=1,
        message=message,
        original_message=message,
        raw_message="",
        font=0,
        sender={"user_id": 123, "nickname": "小明"},
        group_id=100,
        to_me=False,
    )


def test_at_only_message_gets_placeholder() -> None:
    """只 @ 她、不打字：必须给一句占位，否则下游当空消息整条丢掉（@她没反应）。"""
    from dl_senpai.gateway.onebot_adapter import from_onebot_event

    event = _group_event(Message([MessageSegment.at("999")]))
    ctx = from_onebot_event(event, None)
    assert ctx is not None
    assert ctx.is_to_me is True
    assert ctx.plain_text == "（只@了你，没打字）"


def test_at_only_with_image_keeps_empty_text() -> None:
    """只发图（顺带 @ 她）不该被写成"没打字"：图片要走视觉通路。"""
    from dl_senpai.gateway.onebot_adapter import from_onebot_event

    event = _group_event(
        Message([MessageSegment.at("999"), MessageSegment.image("https://example.com/a.png")])
    )
    ctx = from_onebot_event(event, None)
    assert ctx is not None
    assert ctx.plain_text == ""
    assert ctx.has_images() is True


def test_at_other_without_text_keeps_empty() -> None:
    """@ 的是别人、又没打字：跟她无关，不该凭空造出一句话。"""
    from dl_senpai.gateway.onebot_adapter import from_onebot_event

    event = _group_event(Message([MessageSegment.at("12345")]))
    ctx = from_onebot_event(event, None)
    assert ctx is not None
    assert ctx.is_to_me is False
    assert ctx.plain_text == "@12345"


def test_private_empty_message_gets_placeholder() -> None:
    """私聊里空文本同样会被丢掉，所以也给占位。"""
    from nonebot.adapters.onebot.v11 import PrivateMessageEvent
    from dl_senpai.gateway.onebot_adapter import from_onebot_event

    event = PrivateMessageEvent(
        time=1,
        self_id=999,
        post_type="message",
        sub_type="friend",
        user_id=123,
        message_type="private",
        message_id=1,
        message=Message([]),
        original_message=Message([]),
        raw_message="",
        font=0,
        sender={"user_id": 123, "nickname": "小明"},
        to_me=True,
    )
    ctx = from_onebot_event(event, None)
    assert ctx is not None
    assert ctx.plain_text == "（只@了你，没打字）"

async def test_fill_at_names_skips_private_chat() -> None:
    """私聊没有群名片可查，直接原样返回，不做网络请求。"""
    _AT_NAME_CACHE.clear()
    bot = _FakeBot({12345: {"card": "学生甲"}})
    event = _FakeEvent(Message([MessageSegment.at("12345")]), group_id=0)
    out = await _fill_at_names("@12345 在吗", event, bot, SELF_ID)
    assert out == "@12345 在吗"
    assert bot.calls == []
