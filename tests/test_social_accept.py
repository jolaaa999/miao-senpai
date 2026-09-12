from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from nonebot.adapters.onebot.v11 import GroupRequestEvent

from dl_senpai.config import PluginConfig
from dl_senpai import social_accept as mod


def test_auto_accept_defaults_enabled() -> None:
    cfg = PluginConfig()
    assert cfg.auto_accept_friend is True
    assert cfg.auto_accept_group is True


def test_auto_accept_can_disable() -> None:
    cfg = PluginConfig(
        DL_SENPAI_AUTO_ACCEPT_FRIEND=False,
        DL_SENPAI_AUTO_ACCEPT_GROUP=False,
    )
    assert cfg.auto_accept_friend is False
    assert cfg.auto_accept_group is False


def _fake_group_event(*, sub_type: str) -> MagicMock:
    event = MagicMock(spec=GroupRequestEvent)
    event.sub_type = sub_type
    event.group_id = 600478436
    event.user_id = 111
    event.comment = "test"
    event.approve = AsyncMock()
    # isinstance(event, GroupRequestEvent) via MagicMock(spec=...) is True in some versions,
    # but not reliable — force via __class__
    event.__class__ = GroupRequestEvent
    return event


@pytest.mark.asyncio
async def test_group_join_add_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    """入群申请（sub_type=add）即使 auto_accept_group=True 也不同意。"""
    monkeypatch.setattr(
        mod,
        "get_config",
        lambda: PluginConfig(DL_SENPAI_AUTO_ACCEPT_GROUP=True),
    )
    event = _fake_group_event(sub_type="add")
    await mod.handle_social_request(MagicMock(), event)
    event.approve.assert_not_called()


@pytest.mark.asyncio
async def test_group_invite_still_approves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        mod,
        "get_config",
        lambda: PluginConfig(DL_SENPAI_AUTO_ACCEPT_GROUP=True),
    )
    event = _fake_group_event(sub_type="invite")
    await mod.handle_social_request(MagicMock(), event)
    event.approve.assert_awaited_once()
