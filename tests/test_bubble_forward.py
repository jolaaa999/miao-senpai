from __future__ import annotations

import asyncio

import dl_senpai.reply_send as rs_mod
from dl_senpai.reply_send import _send_text_reply_once


class _FakeBot:
    """只记调用，不做真发送；self_id 供合并转发节点使用。"""

    def __init__(self) -> None:
        self.self_id = "10001"
        self.sent: list = []
        self.forwards: list = []
        self.fail_forward = False

    async def send(self, event, message):
        self.sent.append(message)

    async def send_group_forward_msg(self, group_id=None, messages=None):
        if self.fail_forward:
            raise RuntimeError("forward boom")
        self.forwards.append(messages)

    async def send_private_forward_msg(self, user_id=None, messages=None):
        if self.fail_forward:
            raise RuntimeError("forward boom")
        self.forwards.append(messages)


class _FakeGroupEvent:
    group_id = 9527
    user_id = 10086


class _FakePrivateEvent:
    user_id = 10086
    group_id = 9527


def _patch_events(monkeypatch) -> None:
    """_send_forward 用 isinstance 分群/私聊，这里把两个事件类型换成轻量替身。"""
    monkeypatch.setattr(rs_mod, "GroupMessageEvent", _FakeGroupEvent)
    monkeypatch.setattr(rs_mod, "PrivateMessageEvent", _FakePrivateEvent)


def _text_with_bubbles(at_least: int) -> str:
    """造一段会被拆成多个气泡、且合计字数不低于 at_least 的正文。"""
    text = "第一句说点什么。第二句再补一句。第三句收个尾。"
    while len(text) < at_least:
        text += "再多说一句凑字数。"
    return text


async def _send(bot, text: str, total_max: int) -> None:
    await _send_text_reply_once(
        bot, _FakeGroupEvent(), text,
        sticker=None, voice=None, image=None,
        reply_max_chars=1500, reply_chunk_chars=900, nickname="学姐",
        humanize_send=True, bubble_total_max_chars=total_max,
    )


def test_bubbles_below_threshold_still_sent_one_by_one(monkeypatch) -> None:
    _patch_events(monkeypatch)
    bot = _FakeBot()
    asyncio.run(_send(bot, _text_with_bubbles(120), total_max=10000))
    assert bot.forwards == []
    assert len(bot.sent) >= 2


def test_bubbles_above_threshold_sent_as_single_message(monkeypatch) -> None:
    """合计超阈值就整条单发，不再转合并转发卡片（2026-10-25 改）。

    理由：一条 200 字的正经答疑塞进「聊天记录」卡片很突兀，而它本来也不该
    拆成三条。拆泡只服务短句闲聊。
    """
    _patch_events(monkeypatch)
    bot = _FakeBot()
    asyncio.run(_send(bot, _text_with_bubbles(120), total_max=1))
    assert bot.forwards == [], "不再转卡片"
    assert len(bot.sent) == 1, "超阈值应该整条单发"


def test_no_newline_text_stays_one_message(monkeypatch) -> None:
    """require_newline 打开时，单段文本即使能按句切也不拆。"""
    _patch_events(monkeypatch)
    bot = _FakeBot()
    asyncio.run(_send_text_reply_once(
        bot, _FakeGroupEvent(), _text_with_bubbles(120),
        sticker=None, voice=None, image=None,
        reply_max_chars=1500, reply_chunk_chars=900, nickname="学姐",
        humanize_send=True, bubble_total_max_chars=10000,
        bubble_require_newline=True,
    ))
    assert bot.forwards == []
    assert len(bot.sent) == 1


def test_long_text_still_uses_forward(monkeypatch) -> None:
    _patch_events(monkeypatch)
    bot = _FakeBot()
    asyncio.run(_send(bot, "内" * 2000, total_max=700))
    assert len(bot.forwards) == 1


def test_single_bubble_text_never_merges(monkeypatch) -> None:
    """只有一句、拆不出多气泡时，即使超阈值也照常单条发。"""
    _patch_events(monkeypatch)
    bot = _FakeBot()
    asyncio.run(_send(bot, "嗯。", total_max=1))
    assert bot.forwards == []
    assert len(bot.sent) == 1