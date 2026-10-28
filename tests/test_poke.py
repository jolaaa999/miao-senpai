"""拍一拍：解析 / 额度 / 回拍。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dl_senpai.poke import (
    PokeBudget,
    execute_poke,
    handle_poke_notice,
    parse_poke_request,
    reset_poke_budget,
    session_of,
)


class _Cfg:
    poke_enable = True
    poke_cooldown_sec = 90
    poke_max_per_hour = 3
    poke_back_enable = True
    poke_back_probability = 1.0
    poke_back_cooldown_sec = 20
    dry_run = False

    def is_owner(self, user_id):  # noqa: ARG002
        return True


def test_parse_poke_marker_stripped_from_text():
    text, calls = parse_poke_request("戳你 <<<POKE>>>")
    assert text.strip() == "戳你"
    assert len(calls) == 1
    assert calls[0].tool_name == "poke"
    assert calls[0].arguments["target"] == ""


def test_parse_poke_with_target():
    text, calls = parse_poke_request("<<< poke 123456789 >>>")
    assert text.strip() == ""
    assert calls[0].arguments["target"] == "123456789"


def test_session_of_group_and_private():
    assert session_of(group_id=100, user_id=7) == "group:100"
    assert session_of(group_id=0, user_id=7) == "private:7"


def test_budget_cooldown_blocks_second_poke():
    budget = PokeBudget()
    ok, _ = budget.spend("group:1", "9", now=1000.0, cooldown=90, max_per_hour=5)
    assert ok
    ok, reason = budget.spend("group:1", "9", now=1030.0, cooldown=90, max_per_hour=5)
    assert not ok
    assert "秒后" in reason
    ok, _ = budget.spend("group:1", "9", now=1100.0, cooldown=90, max_per_hour=5)
    assert ok


def test_budget_hourly_cap():
    budget = PokeBudget()
    for i in range(3):
        ok, _ = budget.spend("group:1", str(i), now=1000.0 + i, cooldown=0, max_per_hour=3)
        assert ok
    ok, reason = budget.spend("group:1", "99", now=1005.0, cooldown=0, max_per_hour=3)
    assert not ok
    assert "这一小时" in reason


def test_budget_window_expires():
    budget = PokeBudget()
    for i in range(3):
        budget.spend("group:1", str(i), now=1000.0 + i, cooldown=0, max_per_hour=3)
    # 一小时之后窗口清空，可以再戳（最后一条是 1002，3600 秒后即 >4602）
    ok, _ = budget.spend("group:1", "99", now=5000.0, cooldown=0, max_per_hour=3)
    assert ok


def test_allow_back_respects_probability_and_cooldown():
    budget = PokeBudget()
    assert budget.allow_back("group:1", now=100.0, cooldown=0, probability=1.0, rng=lambda: 0.0)
    # 冷却内不再回拍
    assert not budget.allow_back("group:1", now=105.0, cooldown=20, probability=1.0, rng=lambda: 0.0)
    # 概率为 0 永不回拍
    assert not budget.allow_back("group:2", now=100.0, cooldown=0, probability=0.0, rng=lambda: 0.0)


class _Bot:
    self_id = "10000"

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    async def call_api(self, api: str, **params):
        self.calls.append((api, params))
        if self.fail:
            raise RuntimeError("api 不可用")
        return {"status": "ok"}


def _context(cfg, bot, *, group_id="200", user_id="300"):
    return SimpleNamespace(
        config=cfg, bot=bot, session_id="group:200",
        group_id=group_id, user_id=user_id,
    )


@pytest.mark.asyncio
async def test_execute_poke_defaults_to_speaker():
    reset_poke_budget()
    cfg = _Cfg()
    bot = _Bot()
    from dl_senpai.agent.tools.models import ToolCall

    result = await execute_poke(_context(cfg, bot), ToolCall(tool_name="poke"))
    assert result.success
    assert bot.calls and bot.calls[0][1]["user_id"] == 300
    assert bot.calls[0][1]["group_id"] == 200


@pytest.mark.asyncio
async def test_execute_poke_quota_blocks_second():
    reset_poke_budget()
    cfg = _Cfg()
    bot = _Bot()
    from dl_senpai.agent.tools.models import ToolCall

    first = await execute_poke(_context(cfg, bot), ToolCall(tool_name="poke"))
    second = await execute_poke(_context(cfg, bot), ToolCall(tool_name="poke"))
    assert first.success
    assert not second.success
    assert second.error_type == "quota"


@pytest.mark.asyncio
async def test_execute_poke_dry_run_does_not_call_api():
    reset_poke_budget()
    cfg = _Cfg()
    cfg.dry_run = True
    bot = _Bot()
    from dl_senpai.agent.tools.models import ToolCall

    result = await execute_poke(_context(cfg, bot), ToolCall(tool_name="poke"))
    assert result.success
    assert result.data["dry_run"] is True
    assert bot.calls == []


@pytest.mark.asyncio
async def test_execute_poke_refuses_self_target():
    reset_poke_budget()
    cfg = _Cfg()
    bot = _Bot()
    from dl_senpai.agent.tools.models import ToolCall

    call = ToolCall(tool_name="poke", arguments={"target": "10000"})
    result = await execute_poke(_context(cfg, bot), call)
    assert not result.success
    assert result.error_type == "self_target"


def _poke_event(*, user_id: int = 300, target_id: int = 10000, group_id: int = 200):
    """造一条真实的 PokeNotifyEvent。

    用真实事件类而不是 SimpleNamespace：这条路径的价值全在 isinstance 上，
    用假对象测等于没测。
    """
    from nonebot.adapters.onebot.v11 import PokeNotifyEvent

    return PokeNotifyEvent(
        time=1700000000,
        self_id=10000,
        post_type="notice",
        notice_type="notify",
        sub_type="poke",
        user_id=user_id,
        target_id=target_id,
        group_id=group_id,
    )


@pytest.mark.asyncio
async def test_handle_poke_notice_pokes_back(monkeypatch):
    import dl_senpai.config as config_mod

    reset_poke_budget()
    monkeypatch.setattr(config_mod, "get_config", lambda: _Cfg(), raising=False)
    bot = _Bot()
    assert await handle_poke_notice(bot, _poke_event()) is True
    assert bot.calls and bot.calls[0][0] in {"send_poke", "group_poke"}
    assert bot.calls[0][1]["user_id"] == 300


@pytest.mark.asyncio
async def test_handle_poke_notice_ignores_other_peoples_pokes(monkeypatch):
    """别人互相拍：不该接管，也不该回拍。"""
    import dl_senpai.config as config_mod

    reset_poke_budget()
    monkeypatch.setattr(config_mod, "get_config", lambda: _Cfg(), raising=False)
    bot = _Bot()
    assert await handle_poke_notice(bot, _poke_event(target_id=999)) is False
    assert bot.calls == []


@pytest.mark.asyncio
async def test_handle_poke_notice_ignores_non_poke_notice(monkeypatch):
    import dl_senpai.config as config_mod
    from types import SimpleNamespace

    monkeypatch.setattr(config_mod, "get_config", lambda: _Cfg(), raising=False)
    bot = _Bot()
    other = SimpleNamespace(self_id=10000, user_id=300, target_id=10000)
    assert await handle_poke_notice(bot, other) is False
    assert bot.calls == []


@pytest.mark.asyncio
async def test_handle_poke_notice_respects_dry_run(monkeypatch):
    import dl_senpai.config as config_mod

    reset_poke_budget()
    cfg = _Cfg()
    cfg.dry_run = True
    monkeypatch.setattr(config_mod, "get_config", lambda: cfg, raising=False)
    bot = _Bot()
    assert await handle_poke_notice(bot, _poke_event()) is True
    assert bot.calls == []
