"""她自己的生活单测：静默时段、配额、决策解析、发消息闸门。

这个模块一旦判断错，表现不是报错，而是「她半夜突然在群里说话」或者
「一天发了六条」。所以每条安全阀都单独钉一遍。
"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from dl_senpai.own_life import (
    execute,
    in_quiet_hours,
    is_due,
    load_state,
    parse_decision,
    save_state,
    tick,
)


def _dt(hour: int, day: int = 24) -> datetime:
    return datetime(2026, 10, day, hour, 0, 0)


# ---------------------------------------------------------------- 静默时段


def test_quiet_hours_normal_range():
    assert in_quiet_hours(4, start=3, end=8) is True
    assert in_quiet_hours(3, start=3, end=8) is True
    assert in_quiet_hours(8, start=3, end=8) is False
    assert in_quiet_hours(10, start=3, end=8) is False


def test_quiet_hours_wraps_midnight():
    # 23:00–06:00 这种写法必须支持，否则跨零点配置会静默失效
    assert in_quiet_hours(23, start=23, end=6) is True
    assert in_quiet_hours(2, start=23, end=6) is True
    assert in_quiet_hours(12, start=23, end=6) is False


def test_quiet_hours_zero_range_disables():
    assert in_quiet_hours(4, start=5, end=5) is False


# ---------------------------------------------------------------- 该不该叫


def _state(fires: int = 0, day: str = "2026-10-24", last: float = 0.0) -> dict:
    return {"day": day, "fires": fires, "last_fire_at": last}


def test_is_due_when_fresh():
    due, reason = is_due(
        _state(), _dt(14), interval_sec=9000, max_per_day=6, quiet_start=3, quiet_end=8
    )
    assert due is True
    assert reason == "due"


def test_is_due_blocked_in_quiet_hours():
    due, reason = is_due(
        _state(), _dt(4), interval_sec=9000, max_per_day=6, quiet_start=3, quiet_end=8
    )
    assert due is False
    assert reason.startswith("quiet_hours")


def test_is_due_blocked_by_daily_cap():
    due, reason = is_due(
        _state(fires=6), _dt(14), interval_sec=9000, max_per_day=6, quiet_start=3, quiet_end=8
    )
    assert due is False
    assert reason.startswith("daily_cap")


def test_daily_cap_resets_on_new_day():
    """跨天必须清零，否则第二天她就彻底哑了。"""
    due, reason = is_due(
        _state(fires=6, day="2026-10-23"),
        _dt(14),
        interval_sec=9000,
        max_per_day=6,
        quiet_start=3,
        quiet_end=8,
    )
    assert due is True


def test_is_due_blocked_by_interval(monkeypatch):
    import time as _time

    monkeypatch.setattr(_time, "time", lambda: 1_000_000.0)
    due, reason = is_due(
        _state(last=999_000.0),
        _dt(14),
        interval_sec=9000,
        max_per_day=6,
        quiet_start=3,
        quiet_end=8,
    )
    assert due is False
    assert reason.startswith("not_yet")


def test_interval_zero_disables_feature():
    due, reason = is_due(
        _state(), _dt(14), interval_sec=0, max_per_day=6, quiet_start=3, quiet_end=8
    )
    assert due is False
    assert reason == "interval_disabled"


# ---------------------------------------------------------------- 状态


def test_state_roundtrip(tmp_path):
    save_state({"day": "2026-10-24", "fires": 3, "last_fire_at": 123.0}, tmp_path)
    state = load_state(tmp_path)
    assert state["day"] == "2026-10-24"
    assert state["fires"] == 3
    assert state["last_fire_at"] == 123.0


def test_state_missing_returns_clean_default(tmp_path):
    """状态读不出来就当全新一天——不能因为文件坏了就永不唤醒。"""
    state = load_state(tmp_path)
    assert state["fires"] == 0
    assert state["day"] == ""


# ---------------------------------------------------------------- 决策解析


def test_parse_decision_none():
    assert parse_decision('{"action":"none"}')["action"] == "none"


def test_parse_decision_fenced_json():
    raw = '```json\n{"action":"diary","text":"今天有点困","mood":"蔫","tags":["困"]}\n```'
    out = parse_decision(raw)
    assert out["action"] == "diary"
    assert out["text"] == "今天有点困"
    assert out["mood"] == "蔫"


def test_parse_decision_garbage_falls_back_to_none():
    """解析失败必须退化成 none：主动发出去的话撤不回来，宁可不发。"""
    for raw in ("", "我觉得可以聊两句", '{"action":"blah"}', "not json at all"):
        assert parse_decision(raw)["action"] == "none"


def test_parse_decision_strips_newlines():
    # 用 json.dumps 构造，避免手写转义把自己绕进去：
    # 模型真实输出就是带合法 \n 转义的 JSON，解析后正文里的换行要压成空格
    raw = json.dumps({"action": "message", "text": "  在吗\n 睡了吗 "}, ensure_ascii=False)
    out = parse_decision(raw)
    assert out["text"] == "在吗 睡了吗"


# ---------------------------------------------------------------- 执行闸门


class _FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    async def send_group_msg(self, *, group_id: int, message: str) -> None:
        self.sent.append((group_id, message))


class _FakeCfg:
    def __init__(self, groups: str = "") -> None:
        self.own_life_groups = groups
        self.diary_dir = ""
        self.diary_context_n = 3
        self.diary_max_chars = 600


async def test_execute_message_without_whitelist_is_skipped():
    """没配白名单群就绝不发言——这是唯一的外发动作，必须有硬闸门。"""
    bot = _FakeBot()
    result = await execute(bot, _FakeCfg(groups=""), {"action": "message", "text": "在吗"})
    assert result == "message_skipped_no_group"
    assert bot.sent == []


async def test_execute_message_sends_to_whitelisted_group():
    bot = _FakeBot()
    result = await execute(
        bot, _FakeCfg(groups="123456,789"), {"action": "message", "text": "刚吃完饭"}
    )
    assert result == "message"
    assert len(bot.sent) == 1
    assert bot.sent[0][1] == "刚吃完饭"
    assert bot.sent[0][0] in (123456, 789)


async def test_execute_message_empty_text_is_skipped():
    bot = _FakeBot()
    result = await execute(bot, _FakeCfg(groups="123"), {"action": "message", "text": "   "})
    assert result == "message_skipped_empty"
    assert bot.sent == []


async def test_execute_message_strips_markdown_and_caps_length():
    bot = _FakeBot()
    await execute(bot, _FakeCfg(groups="123"), {"action": "message", "text": "a" * 500})
    assert len(bot.sent[0][1]) <= 120


async def test_execute_none_does_nothing():
    bot = _FakeBot()
    assert await execute(bot, _FakeCfg(), {"action": "none"}) == "none"
    assert bot.sent == []


async def test_tick_baselines_on_first_run(tmp_path, monkeypatch):
    """首次启用只记基线，不当场叫醒——打开开关不该等于「立刻发一条」。"""
    import dl_senpai.own_life as mod

    class _Cfg:
        own_life_enable = True
        own_life_dir = str(tmp_path)
        own_life_interval_sec = 9000
        own_life_max_per_day = 6
        own_life_quiet_start = 3
        own_life_quiet_end = 8
        diary_dir = str(tmp_path)
        diary_context_n = 3
        diary_max_chars = 600

    monkeypatch.setattr("dl_senpai.config.get_config", lambda: _Cfg(), raising=False)

    # 首次调用：不调用模型、不发消息，只写基线
    assert await mod.tick(_FakeBot(), now=datetime(2026, 10, 24, 14)) == ""
    state = load_state(tmp_path)
    assert state["last_fire_at"] > 0
    assert state["fires"] == 0


async def test_tick_noop_when_disabled(monkeypatch):
    """功能默认关闭：关着的时候 tick 不能碰配置以外的任何东西。"""
    import dl_senpai.own_life as mod

    class _Cfg:
        own_life_enable = False

    monkeypatch.setattr(
        "dl_senpai.config.get_config", lambda: _Cfg(), raising=False
    )
    assert await mod.tick(_FakeBot()) == ""
