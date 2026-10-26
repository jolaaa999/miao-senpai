"""撤回通知：网关归一化 + 流水标记 + 她自己被撤回时的接话。

为什么值得测：撤回是唯一一种「她之前说过的话被抹掉」的事件。
处理错了两类后果都很显眼——她继续引用已经被撤回的内容，或者追着每条撤回喊一嗓子。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dl_senpai import recall_notice
from dl_senpai.gateway.context import (
    NOTICE_FRIEND_RECALL,
    NOTICE_GROUP_RECALL,
    MessageContext,
)
from dl_senpai.recent_chat import RecentChatStore

SELF_ID = "999"


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_group_msg(self, *, group_id: int, message: str):
        self.sent.append({"group_id": group_id, "message": message})
        return {"message_id": 1}


class _FakeGuard:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.success = 0
        self.failure = 0

    def before_send(self):
        return self.allowed, "" if self.allowed else "breaker_open"

    def on_success(self) -> None:
        self.success += 1

    def on_failure(self) -> None:
        self.failure += 1


def _cfg(**over: object) -> SimpleNamespace:
    base = dict(recall_reply_enable=True, recall_reply_cooldown=180, dry_run=False)
    base.update(over)
    return SimpleNamespace(**base)


def _ctx(*, notice_type: str = NOTICE_GROUP_RECALL, author: str = "123", operator: str = "123",
         message_id: str = "m1", group_id: str = "100", bot=None) -> MessageContext:
    ctx = MessageContext(
        message_type="notice",
        session_id=f"group:{group_id}" if group_id else "",
        group_id=group_id,
        self_id=SELF_ID,
        notice_type=notice_type,
        recalled_message_id=message_id,
        recall_author_id=author,
        recall_operator_id=operator,
    )
    ctx.bot = bot
    return ctx


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    """每个用例都用独立的流水目录与空冷却表，避免互相污染。"""
    recall_notice.reset_cooldown()
    store = RecentChatStore(tmp_path, ttl_sec=3600)
    monkeypatch.setattr(recall_notice, "get_recent_chat_store", lambda *a, **k: store)
    monkeypatch.setattr("dl_senpai.send_guard.get_send_guard", lambda *a, **k: _FakeGuard())
    return store


# -- 网关归一化 --


def test_group_recall_normalized() -> None:
    """群撤回要带出「作者 / 操作者 / 消息 id」三个字段，后面全靠它们判断。"""
    from nonebot.adapters.onebot.v11 import GroupRecallNoticeEvent
    from dl_senpai.gateway.onebot_adapter import from_onebot_event

    event = GroupRecallNoticeEvent(
        time=1, self_id=int(SELF_ID), post_type="notice", notice_type="group_recall",
        user_id=2843422418, group_id=1056066897, operator_id=10001, message_id=830158182,
    )
    ctx = from_onebot_event(event, None)
    assert ctx is not None
    assert ctx.notice_type == NOTICE_GROUP_RECALL
    assert ctx.recall_author_id == "2843422418"
    assert ctx.recall_operator_id == "10001"
    assert ctx.recalled_message_id == "830158182"
    assert ctx.session_id == "group:1056066897"
    assert ctx.is_private is False


def test_friend_recall_normalized() -> None:
    from nonebot.adapters.onebot.v11 import FriendRecallNoticeEvent
    from dl_senpai.gateway.onebot_adapter import from_onebot_event

    event = FriendRecallNoticeEvent(
        time=1, self_id=int(SELF_ID), post_type="notice", notice_type="friend_recall",
        user_id=555, message_id=42,
    )
    ctx = from_onebot_event(event, None)
    assert ctx is not None
    assert ctx.notice_type == NOTICE_FRIEND_RECALL
    assert ctx.session_id == "private:555"
    assert ctx.is_private is True


def test_other_notice_stays_other() -> None:
    """别的通知不能被误判成撤回（否则会把入群欢迎之类打断）。"""
    from nonebot.adapters.onebot.v11 import NoticeEvent
    from dl_senpai.gateway.onebot_adapter import from_onebot_event
    from dl_senpai.gateway.context import NOTICE_OTHER

    event = NoticeEvent(
        time=1, self_id=int(SELF_ID), post_type="notice", notice_type="notify",
    )
    ctx = from_onebot_event(event, None)
    assert ctx is not None
    assert ctx.notice_type == NOTICE_OTHER
    assert ctx.recalled_message_id == ""


# -- 流水标记 --


def test_mark_recalled_marks_entry(_clean_state) -> None:
    store = _clean_state
    store.append("group:100", sender="小明", user_id="1", text="我说句话", message_id="m1")
    assert store.mark_recalled("group:100", "m1") is True
    entries = store.entries("group:100")
    assert entries[0].text.endswith("（已撤回）")
    # 重复标记不叠加
    assert store.mark_recalled("group:100", "m1") is True
    assert store.entries("group:100")[0].text.count("（已撤回）") == 1


def test_mark_recalled_unknown_id(_clean_state) -> None:
    assert _clean_state.mark_recalled("group:100", "nope") is False


# -- 别人的撤回 --


@pytest.mark.asyncio
async def test_other_recall_only_marks(_clean_state, monkeypatch) -> None:
    """别人撤回自己消息时她不该开口，只把流水标掉。"""
    called: list[str] = []

    async def _boom(*a, **k):
        called.append("compose")
        return "不该被调用"

    monkeypatch.setattr(recall_notice, "_compose", _boom)
    store = _clean_state
    store.append("group:100", sender="小明", user_id="123", text="说错话了", message_id="m1")
    bot = _FakeBot()
    result = await recall_notice.handle_recall(_ctx(author="123", operator="123", bot=bot), config=_cfg())
    assert result == "other_marked"
    assert called == []
    assert bot.sent == []
    assert store.entries("group:100")[0].text.endswith("（已撤回）")


# -- 她自己的消息被撤回 --


@pytest.mark.asyncio
async def test_own_recall_replies(monkeypatch) -> None:
    """作者是她自己时允许说一句，且用的是群发而不是随口调用。"""
    async def _compose(cfg, *, operator_is_self: bool) -> str:
        assert operator_is_self is False  # 管理员撤的
        return "怎么把我也撤回了"

    monkeypatch.setattr(recall_notice, "_compose", _compose)
    bot = _FakeBot()
    result = await recall_notice.handle_recall(
        _ctx(author=SELF_ID, operator="10001", bot=bot), config=_cfg()
    )
    assert result == "own_replied"
    assert bot.sent == [{"group_id": 100, "message": "怎么把我也撤回了"}]


@pytest.mark.asyncio
async def test_own_recall_cooldown(monkeypatch) -> None:
    """冷却期内第二次撤回不再开口——群里连撤十条也只说一句。"""
    async def _compose(cfg, *, operator_is_self: bool) -> str:
        return "喂"

    monkeypatch.setattr(recall_notice, "_compose", _compose)
    bot = _FakeBot()
    first = await recall_notice.handle_recall(_ctx(author=SELF_ID, bot=bot), config=_cfg())
    second = await recall_notice.handle_recall(_ctx(author=SELF_ID, message_id="m2", bot=bot), config=_cfg())
    assert first == "own_replied"
    assert second == "own_cooldown"
    assert len(bot.sent) == 1


@pytest.mark.asyncio
async def test_own_recall_disabled(monkeypatch) -> None:
    async def _compose(cfg, *, operator_is_self: bool) -> str:
        return "喂"

    monkeypatch.setattr(recall_notice, "_compose", _compose)
    bot = _FakeBot()
    result = await recall_notice.handle_recall(_ctx(author=SELF_ID, bot=bot), config=_cfg(recall_reply_enable=False))
    assert result == "own_silent"
    assert bot.sent == []


@pytest.mark.asyncio
async def test_own_recall_friend_chat_skipped(monkeypatch) -> None:
    """私聊里被撤回不接话：没有群语境，容易变成打扰。"""
    async def _compose(cfg, *, operator_is_self: bool) -> str:
        return "喂"

    monkeypatch.setattr(recall_notice, "_compose", _compose)
    bot = _FakeBot()
    ctx = _ctx(notice_type=NOTICE_FRIEND_RECALL, author=SELF_ID, group_id="", bot=bot)
    assert await recall_notice.handle_recall(ctx, config=_cfg()) == "own_private_skipped"


@pytest.mark.asyncio
async def test_own_recall_blocked_by_guard(monkeypatch) -> None:
    """熔断开着就闭嘴，别在风控期硬发。"""
    async def _compose(cfg, *, operator_is_self: bool) -> str:
        return "喂"

    monkeypatch.setattr(recall_notice, "_compose", _compose)
    monkeypatch.setattr("dl_senpai.send_guard.get_send_guard", lambda *a, **k: _FakeGuard(allowed=False))
    bot = _FakeBot()
    result = await recall_notice.handle_recall(_ctx(author=SELF_ID, bot=bot), config=_cfg())
    assert result == "own_blocked"
    assert bot.sent == []


@pytest.mark.asyncio
async def test_own_recall_compose_failure_silent(monkeypatch) -> None:
    """生成失败就什么都不说，不能发出半成品。"""
    async def _boom(cfg, *, operator_is_self: bool) -> str:
        raise RuntimeError("relay 502")

    monkeypatch.setattr(recall_notice, "_compose", _boom)
    bot = _FakeBot()
    assert await recall_notice.handle_recall(_ctx(author=SELF_ID, bot=bot), config=_cfg()) == "own_compose_failed"
    assert bot.sent == []


def test_cooldown_left_math() -> None:
    recall_notice.reset_cooldown()
    assert recall_notice.cooldown_left("group:100", 0) == 0.0
    assert recall_notice.cooldown_left("", 180) == 0.0
    recall_notice._LAST_REPLY_AT["group:100"] = 1000.0
    assert recall_notice.cooldown_left("group:100", 180, now=1060.0) == 120.0
    assert recall_notice.cooldown_left("group:100", 180, now=1200.0) == 0.0
