"""B站「刷新首页」：每轮都要拿新的一批视频。

2026-10-27 的问题：bili_hot 固定打 popular?pn=1，热榜几小时不动，
她就同一个 BV 点了 7 次。现在改成 bili_feed：

1. 优先首页推荐流（实测连叫两次重合 0 条 = 真「刷新页面」）；
2. 推荐流挂了退回热门**随机页**（pn 1~5，实测页间重合也是 0）；
3. fresh_idx 也随机，等于「往下滑了几屏」。
"""

from __future__ import annotations

import asyncio

from dl_senpai import bili as bl


class Cfg:
    bili_timeout = 5.0


def _rcmd_item(bvid: str, title: str = "视频") -> dict:
    return {
        "bvid": bvid,
        "id": 1,
        "title": title,
        "duration": 60,
        "owner": {"name": "某UP"},
        "stat": {"view": 10000, "like": 100, "danmaku": 5},
    }


def _popular_item(bvid: str) -> dict:
    return {
        "bvid": bvid,
        "aid": 1,
        "title": "热门视频",
        "owner": {"name": "热门UP"},
        "stat": {"view": 20000},
    }


def test_prefers_rcmd(monkeypatch) -> None:
    seen: list[dict] = []

    async def _get(path, params, *, timeout, cookies=None):
        seen.append({"path": path, "params": dict(params)})
        if "rcmd" in path:
            return {"item": [_rcmd_item("BV1" + str(i)) for i in range(4)]}
        return {"list": [_popular_item("BV9")]}

    monkeypatch.setattr(bl, "_get", _get)
    videos, source = asyncio.run(bl.bili_feed(limit=4, cfg=Cfg()))
    assert source == "rcmd"
    assert [v.bvid for v in videos] == ["BV10", "BV11", "BV12", "BV13"]
    # fresh_idx 必须随机：写死一个值就又变成「永远同一批」
    assert 1 <= seen[0]["params"]["fresh_idx"] <= 6


def test_falls_back_to_random_popular_page(monkeypatch) -> None:
    seen: list[dict] = []

    async def _get(path, params, *, timeout, cookies=None):
        seen.append({"path": path, "params": dict(params)})
        if "rcmd" in path:
            raise bl.BiliError("rcmd 挂了")
        return {"list": [_popular_item("BV7") for _ in range(3)]}

    monkeypatch.setattr(bl, "_get", _get)
    videos, source = asyncio.run(bl.bili_feed(limit=3, cfg=Cfg()))
    assert source == "popular"
    assert [v.bvid for v in videos] == ["BV7", "BV7", "BV7"]
    # 退路也必须是随机页，pn=1 就是原来那个 bug
    assert 1 <= seen[-1]["params"]["pn"] <= 5


def test_rcmd_too_few_falls_back(monkeypatch) -> None:
    async def _get(path, params, *, timeout, cookies=None):
        if "rcmd" in path:
            # 只回 1 条基本就是异常返回，别拿它当整轮结果
            return {"item": [_rcmd_item("BV1only")]}
        return {"list": [_popular_item("BV8") for _ in range(3)]}

    monkeypatch.setattr(bl, "_get", _get)
    _videos, source = asyncio.run(bl.bili_feed(limit=3, cfg=Cfg()))
    assert source == "popular"
