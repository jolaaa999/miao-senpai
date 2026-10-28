"""刷帖调度：作息权重 / 静默期 / 每日上限的测试。

为什么钉这些：
频率是拿去"骗"平台的，一旦调度写错（比如凌晨也在刷、或者一天刷 40 次），
后果不是难看，是被限流封号。所以边界必须钉死。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from dl_senpai import heybox_browse as hb


class Cfg:
    heybox_browse_enable = True
    heybox_browse_interval_min_sec = 3000
    heybox_browse_interval_max_sec = 8400
    heybox_browse_daily_max = 16
    heybox_browse_burst_probability = 0.0
    heybox_captcha_backoff_sec = 10800
    heybox_browse_schedule_file = ""
    own_life_quiet_start = 3
    own_life_quiet_end = 8


def test_hour_weight_peaks_at_night() -> None:
    # 晚上比白天密，白天比凌晨密；静默时段权重为 0
    assert hb._hour_weight(21) > hb._hour_weight(15) > hb._hour_weight(1)
    assert hb._hour_weight(3) == 0.0
    assert hb._hour_weight(7) == 0.0


def test_hour_weight_unknown_hour_is_neutral() -> None:
    # 表里没写的小时退回 1.0，而不是 0：否则漏一小时就永远不刷了
    # 越界小时按 24 取模：30 点 == 6 点（静默，权重 0）
    assert hb._hour_weight(30) == hb._hour_weight(6) == 0.0
    assert hb._hour_weight(24) == hb._hour_weight(0)


def test_quiet_hours_window() -> None:
    cfg = Cfg()
    assert hb._in_quiet_hours(cfg, datetime(2026, 10, 26, 4, 30)) is True
    assert hb._in_quiet_hours(cfg, datetime(2026, 10, 26, 8, 0)) is False
    assert hb._in_quiet_hours(cfg, datetime(2026, 10, 26, 21, 0)) is False


def test_gap_respects_bounds(monkeypatch) -> None:
    cfg = Cfg()
    # 固定到白天普通时段，避免静默期分支
    monkeypatch.setattr(hb, "datetime", _at(10, 26, 15, 0))
    for _ in range(50):
        gap = hb._next_gap_seconds(cfg)
        assert 60 <= gap <= 5 * 3600


def test_gap_shorter_at_peak_than_small_hours(monkeypatch) -> None:
    cfg = Cfg()
    monkeypatch.setattr(hb, "datetime", _at(10, 26, 21, 0))
    night = [hb._next_gap_seconds(cfg) for _ in range(30)]
    monkeypatch.setattr(hb, "datetime", _at(10, 26, 1, 0))
    small = [hb._next_gap_seconds(cfg) for _ in range(30)]
    # 平均值：晚上应该明显比凌晨短
    assert sum(night) / len(night) < sum(small) / len(small)


def test_quiet_hours_sleeps_until_quiet_end(monkeypatch) -> None:
    cfg = Cfg()
    monkeypatch.setattr(hb, "datetime", _at(10, 26, 5, 0))
    gap = hb._next_gap_seconds(cfg)
    # 5 点离 8 点还有 3 小时，加上随机几分钟
    assert 3 * 3600 <= gap <= 3 * 3600 + 900


def test_daily_count_resets_next_day(tmp_path, monkeypatch) -> None:
    cfg = Cfg()
    cfg.heybox_browse_schedule_file = str(tmp_path / "sched.json")
    assert hb._today_count(cfg) == 0
    assert hb._bump_today(cfg) == 1
    assert hb._bump_today(cfg) == 2
    assert hb._today_count(cfg) == 2
    # 换一天：归零。
    # 「明天」必须按今天算，不能硬编码日期——写死 10-27 的话，
    # 真实日期走到那天时本测试会挂（那天写进去的计数正好是同一个 key）。
    tomorrow = datetime.now() + timedelta(days=1)
    monkeypatch.setattr(hb, "datetime", _at(tomorrow.month, tomorrow.day, 9, 0))
    assert hb._today_count(cfg) == 0


class _FakeDatetime(datetime):
    """把 datetime.now() 钉在某个时刻，其它行为照旧。"""

    _pinned: datetime = datetime(2026, 10, 26, 12, 0)

    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return cls._pinned


def _at(month: int, day: int, hour: int, minute: int = 0):
    """生成一个"现在就是这个点"的假 datetime 类。"""

    class _Pinned(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102
            return datetime(2026, month, day, hour, minute)

    return _Pinned


def test_gap_never_exceeds_hard_cap(monkeypatch) -> None:
    """主人要的是「每两小时必刷一篇」，所以基础间隔再大也不许超过上限。"""
    from dl_senpai import heybox_browse as hb

    class Cfg:
        heybox_browse_interval_min_sec = 3600
        heybox_browse_interval_max_sec = 9600
        heybox_browse_burst_probability = 0.0
        heybox_browse_max_gap_sec = 7200
        own_life_quiet_start = 3
        own_life_quiet_end = 8

    cfg = Cfg()
    # 低谷时段（权重小）也要被硬上限压住
    monkeypatch.setattr(hb, '_hour_weight', lambda hour: 0.2)
    monkeypatch.setattr(hb, '_seconds_until_quiet_end', lambda cfg, now=None: 0)
    for _ in range(200):
        assert hb._next_gap_seconds(cfg) <= 7200


def test_hard_cap_can_be_lowered(monkeypatch) -> None:
    from dl_senpai import heybox_browse as hb

    class Cfg:
        heybox_browse_interval_min_sec = 3600
        heybox_browse_interval_max_sec = 9600
        heybox_browse_burst_probability = 0.0
        heybox_browse_max_gap_sec = 1800
        own_life_quiet_start = 3
        own_life_quiet_end = 8

    monkeypatch.setattr(hb, '_hour_weight', lambda hour: 0.2)
    monkeypatch.setattr(hb, '_seconds_until_quiet_end', lambda cfg, now=None: 0)
    for _ in range(100):
        assert hb._next_gap_seconds(Cfg()) <= 1800
