"""入群欢迎里的「认证」话术，只发给"学姐真能管"的群。

为什么单独测：这段文案承诺「十分钟内不认证就踢出」。学姐不是群管时踢不了人，
「认证」两个字也接不住（命令 guard 要求待认证状态），于是新人照着做之后，
她只能现场瞎编（线上出现过"学姐这儿只认画图订单，不认身份认证"）。
这条逻辑错不会报错，只会让群里看着像 AI 在胡说。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dl_senpai import handlers


class _FakeBot:
    """只实现入群欢迎会用到的那两个接口。"""

    self_id = "999"

    def __init__(self, role: str) -> None:
        self.role = role
        self.sent: list[str] = []

    async def get_group_member_info(self, *, group_id: int, user_id: int, **kwargs):
        # 查她自己返回配置的角色，查新人就当普通成员
        role = self.role if int(user_id) == int(self.self_id) else "member"
        return {"role": role, "nickname": "小橙子", "card": ""}

    async def send(self, event, message) -> None:
        self.sent.append(str(message))


class _FakeVerifyStore:
    """记录登记动作，不落盘、不挂真实的定时踢人任务。"""

    def __init__(self) -> None:
        self.registered: list[dict] = []

    def register(self, **kwargs):
        self.registered.append(kwargs)
        return SimpleNamespace(
            group_id=kwargs["group_id"],
            user_id=kwargs["user_id"],
            nickname=kwargs["nickname"],
            deadline=0.0,
        )

    def schedule_kick(self, bot, item) -> None:
        return None


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        is_group_allowed=lambda gid: True,
        is_verify_group=lambda gid: True,
        verify_timeout_sec=600,
        verify_dir="data/dl_senpai/verify",
    )


def _event() -> SimpleNamespace:
    return SimpleNamespace(group_id=123, user_id=456, self_id=999)


def _patch(monkeypatch, *, store: _FakeVerifyStore) -> None:
    monkeypatch.setattr(handlers, "get_config", _config)
    # 只看"群管判定"这条逻辑：功能开关一律视为开
    monkeypatch.setattr(handlers, "is_feature_enabled", lambda cfg, gid, key: True)
    monkeypatch.setattr(handlers, "get_verify_store", lambda path: store)


@pytest.mark.asyncio
async def test_no_verify_promise_when_bot_is_not_admin(monkeypatch) -> None:
    """学姐只是普通成员：欢迎词不许提「认证」，也不该登记待认证/挂踢人任务。"""
    store = _FakeVerifyStore()
    _patch(monkeypatch, store=store)
    bot = _FakeBot(role="member")

    await handlers.handle_group_increase(bot, _event())

    assert len(bot.sent) == 1
    assert "认证" not in bot.sent[0]
    assert "踢" not in bot.sent[0]
    assert store.registered == []


@pytest.mark.asyncio
async def test_verify_promise_when_bot_is_admin(monkeypatch) -> None:
    """学姐是管理员：欢迎词带「认证」+ 时限，并登记待认证。"""
    store = _FakeVerifyStore()
    _patch(monkeypatch, store=store)
    bot = _FakeBot(role="admin")

    await handlers.handle_group_increase(bot, _event())

    assert len(bot.sent) == 1
    assert "认证" in bot.sent[0]
    assert "分钟" in bot.sent[0]
    assert len(store.registered) == 1
    assert store.registered[0]["user_id"] == 456


@pytest.mark.asyncio
async def test_group_owner_also_gets_verify_promise(monkeypatch) -> None:
    """群主身份同样能踢人，按群管处理。"""
    store = _FakeVerifyStore()
    _patch(monkeypatch, store=store)
    bot = _FakeBot(role="owner")

    await handlers.handle_group_increase(bot, _event())

    assert "认证" in bot.sent[0]
    assert len(store.registered) == 1
