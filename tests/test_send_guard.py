"""发送闸门与「正在输入」单测。

这两块是防封号的，坏掉不会报错——只会表现为「某天号没了」。
所以每条规则都单独钉一遍，尤其是「熔断期间真的不去碰服务器」。
"""

from __future__ import annotations

import time

import pytest

from dl_senpai.send_guard import CircuitBreaker, RateWindow, SendGuard, reset_send_guard
from dl_senpai.typing_status import TypingReporter, reset_typing_reporter


# ---------------------------------------------------------------- 熔断器


def test_breaker_allows_by_default():
    b = CircuitBreaker(threshold=3, cooldown_sec=180)
    assert b.allow() == (True, "")


def test_breaker_opens_after_threshold():
    b = CircuitBreaker(threshold=3, cooldown_sec=180)
    b.on_failure()
    b.on_failure()
    assert b.allow()[0] is True, "没到阈值不该熔断"

    b.on_failure()
    allowed, reason = b.allow()
    assert allowed is False
    assert "熔断" in reason


def test_breaker_success_resets_counter():
    b = CircuitBreaker(threshold=3, cooldown_sec=180)
    b.on_failure()
    b.on_failure()
    b.on_success()
    b.on_failure()
    b.on_failure()
    assert b.allow()[0] is True, "中间成功过，连续计数应该清零"


def test_breaker_closes_after_cooldown():
    b = CircuitBreaker(threshold=1, cooldown_sec=0.01)
    b.on_failure()
    time.sleep(0.02)
    assert b.allow()[0] is True


def test_breaker_status_reports_remaining():
    b = CircuitBreaker(threshold=1, cooldown_sec=180)
    b.on_failure()
    status = b.status()
    assert status["open"] is True
    assert 0 < status["remain_sec"] <= 180
    assert status["opened_count"] == 1


# ---------------------------------------------------------------- 限流


def test_rate_window_per_minute():
    r = RateWindow(per_minute=2, per_hour=100)
    assert r.allow()[0] is True
    r.record()
    r.record()
    allowed, reason = r.allow()
    assert allowed is False
    assert "每分钟" in reason


def test_rate_window_per_hour():
    r = RateWindow(per_minute=0, per_hour=3)
    for _ in range(3):
        r.record()
    allowed, reason = r.allow()
    assert allowed is False
    assert "每小时" in reason


def test_rate_window_expires_old_stamps():
    r = RateWindow(per_minute=1, per_hour=100)
    now = 1_000_000.0
    r.record(now - 120)  # 两分钟前，已经出窗口
    assert r.allow(now)[0] is True
    r.record(now)
    assert r.allow(now)[0] is False


def test_rate_zero_means_unlimited():
    r = RateWindow(per_minute=0, per_hour=0)
    for _ in range(50):
        r.record()
    assert r.allow()[0] is True


# ---------------------------------------------------------------- 组合闸门


class _Cfg:
    send_breaker_enable = True
    send_breaker_fail_threshold = 2
    send_breaker_cooldown_sec = 180
    send_rate_per_minute = 2
    send_rate_per_hour = 100
    typing_status_enable = False
    typing_status_min_chars = 20
    typing_status_min_interval_sec = 45
    typing_status_probability = 1.0


def test_guard_blocks_when_breaker_open():
    g = SendGuard(_Cfg())
    assert g.before_send()[0] is True
    g.on_failure()
    g.on_failure()
    allowed, reason = g.before_send()
    assert allowed is False
    assert "熔断" in reason


def test_guard_counts_success_against_rate():
    g = SendGuard(_Cfg())
    g.on_success()
    g.on_success()
    allowed, reason = g.before_send()
    assert allowed is False
    assert "每分钟" in reason


def test_guard_disabled_lets_everything_through():
    class _Off(_Cfg):
        send_breaker_enable = False

    g = SendGuard(_Off())
    g.on_failure()
    g.on_failure()
    g.on_failure()
    assert g.before_send()[0] is True


# ---------------------------------------------------------------- 正在输入


class _FakeBot:
    def __init__(self, *, fail: str | None = None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail

    async def call_api(self, api: str, **params):
        self.calls.append((api, params))
        if self.fail:
            raise RuntimeError(self.fail)
        return {"status": "ok"}


class _Event:
    def __init__(self, group_id: int | None = 123) -> None:
        self.group_id = group_id
        self.user_id = 456


class _TypingCfg:
    typing_status_enable = True
    typing_status_min_chars = 5
    typing_status_min_interval_sec = 0
    typing_status_probability = 1.0


async def test_typing_reports_in_group():
    bot = _FakeBot()
    reporter = TypingReporter(_TypingCfg())
    assert await reporter.start(bot, _Event(group_id=980229149), "这是一句够长的话") is True
    assert bot.calls[0][0] == "set_input_status"
    assert bot.calls[0][1] == {"group_id": 980229149, "event_type": 1}
    await reporter.stop(bot, _Event(group_id=980229149))
    assert bot.calls[1][1]["event_type"] == 0


async def test_typing_skips_short_text():
    """「嗯」「在」这种短句不该装打字——真人也不打。"""
    bot = _FakeBot()
    reporter = TypingReporter(_TypingCfg())
    assert await reporter.start(bot, _Event(), "嗯") is False
    assert bot.calls == []


async def test_typing_respects_interval():
    bot = _FakeBot()
    cfg = _TypingCfg()
    cfg.typing_status_min_interval_sec = 45
    reporter = TypingReporter(cfg)
    assert await reporter.start(bot, _Event(), "第一句够长的话") is True
    assert await reporter.start(bot, _Event(), "第二句够长的话") is False
    assert len(bot.calls) == 1


async def test_typing_disabled_by_config():
    bot = _FakeBot()
    cfg = _TypingCfg()
    cfg.typing_status_enable = False
    assert await TypingReporter(cfg).start(bot, _Event(), "够长的一句话") is False
    assert bot.calls == []


async def test_typing_permanently_disabled_when_unsupported():
    """NapCat 不一定支持这个扩展接口：探到一次就永久关掉，别每句都撞一次。"""
    bot = _FakeBot(fail="ActionFailed 1404 not supported")
    reporter = TypingReporter(_TypingCfg())

    assert await reporter.start(bot, _Event(), "够长的一句话") is False
    assert reporter.status()["supported"] is False

    before = len(bot.calls)
    assert await reporter.start(bot, _Event(), "够长的第二句话") is False
    assert len(bot.calls) == before, "已判定不支持后不该再发请求"


def test_singletons_resettable():
    reset_send_guard()
    reset_typing_reporter()
