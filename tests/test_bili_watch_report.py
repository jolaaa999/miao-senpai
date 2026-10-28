"""播放心跳上报：让她的「看」出现在 B 站历史里。

为什么要这个：B 站历史只认播放器上报，她原来只调 view / player / 字幕接口，
平台当她没看过，主人翻历史一条都看不到；观看时长也不算经验，Lv2 就永远差那点。

实测（2026-10-27 20:48）：click/web/h5 + web/heartbeat 发完，历史第一条就是那支视频。
这里钉住：两个上报都要发、播的秒数要落在视频时长之内、没有 DedeUserID 要明确报错。
"""

from __future__ import annotations

import asyncio

import pytest

from dl_senpai import bili as bl


class Cfg:
    bili_timeout = 5.0


def _setup(monkeypatch, *, duration=600, history_has=True, uid="3706995521620369"):
    posts: list[tuple[str, dict]] = []
    gets: list[str] = []

    async def _get(path, params, *, timeout, cookies=None):
        gets.append(path)
        if path.endswith("/view"):
            return {"bvid": params.get("bvid") or "BV1aFno6wETM", "aid": 11, "cid": 22, "duration": duration}
        return {"list": [{"history": {"bvid": "BV1aFno6wETM" if history_has else "BV9"}}]}

    async def _post(path, payload, *, cfg=None, timeout=25.0, referer="", action="", extra_hints=None):
        posts.append((action, dict(payload)))
        return {}

    monkeypatch.setattr(bl, "_get", _get)
    monkeypatch.setattr(bl, "_post", _post)
    monkeypatch.setattr(bl, "_state_cookies", lambda cfg: {"DedeUserID": uid} if uid else {})
    return posts, gets


def test_two_reports_and_history_check(monkeypatch) -> None:
    posts, gets = _setup(monkeypatch)
    result = asyncio.run(bl.bili_watch_report("BV1aFno6wETM", cfg=Cfg(), seconds=120))
    assert result["ok"] and result["played"] == 120
    assert result["in_history"] is True
    actions = [action for action, _payload in posts]
    assert actions == ["上报起播", "上报播放进度"]
    progress = posts[1][1]
    assert progress["aid"] == 11 and progress["cid"] == 22
    assert progress["played_time"] == progress["max_play_progress_time"] == 120
    # 上报成功不等于平台真记，所以要回头看历史
    assert any("history/cursor" in g for g in gets)


def test_played_is_clamped_to_duration(monkeypatch) -> None:
    posts, _gets = _setup(monkeypatch, duration=90)
    result = asyncio.run(bl.bili_watch_report("BV1aFno6wETM", cfg=Cfg(), seconds=99999))
    # 视频只有 90 秒，报 99999 秒一眼是假的
    assert result["played"] == 90


def test_random_when_seconds_zero(monkeypatch) -> None:
    _posts, _gets = _setup(monkeypatch)
    result = asyncio.run(bl.bili_watch_report("BV1aFno6wETM", cfg=Cfg(), seconds=0))
    assert 60 <= result["played"] <= 300


def test_no_uid_raises(monkeypatch) -> None:
    _setup(monkeypatch, uid="")
    with pytest.raises(bl.BiliError):
        asyncio.run(bl.bili_watch_report("BV1aFno6wETM", cfg=Cfg(), seconds=60))


def test_verify_off_skips_history(monkeypatch) -> None:
    _posts, gets = _setup(monkeypatch)
    result = asyncio.run(bl.bili_watch_report("BV1aFno6wETM", cfg=Cfg(), seconds=60, verify=False))
    assert "in_history" not in result
    assert not any("history/cursor" in g for g in gets)
