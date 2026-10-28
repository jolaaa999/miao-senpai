"""字幕串台的防护。

2026-10-27 实况：同一个 BV、同一个 cid、同一个字幕 id，连着问三次拿到三段
完全不同的内容（一次讲珍妮·古道尔，一次是视频本身，一次在讲"我的兴趣爱好"）。
她那天的日记因此写成了"字幕讲的是锅包肉，跟标题对不上"。

结论：B 站那边的 AI 字幕对象会串，**不能信单次结果**。两次一致才认，
否则当没字幕——宁可不给，也不给错的。
"""

from __future__ import annotations

import asyncio

from dl_senpai import bili as bl


class Cfg:
    bili_timeout = 5.0


def _patch(monkeypatch, bodies: list[list[dict]]):
    """让 _get 假装是 B 站，_fetch_subtitle_body 依次返回 bodies 里的内容。"""
    async def _get(path, params, *, timeout, cookies=None):
        if path.endswith("/view"):
            return {"bvid": "BV1btH36uEru", "cid": 43030413417}
        return {"subtitle": {"subtitles": [{"lan": "ai-zh", "subtitle_url": "//x/y"}]}}

    queue = list(bodies)

    async def _fetch(url, *, timeout, cfg=None):
        return queue.pop(0) if queue else []

    monkeypatch.setattr(bl, "_get", _get)
    monkeypatch.setattr(bl, "_fetch_subtitle_body", _fetch)
    monkeypatch.setattr(bl, "_state_cookies", lambda cfg: {})


def _cues(text: str) -> list[dict]:
    return [{"from": i, "to": i + 1, "content": text} for i in range(3)]


def test_same_twice_is_accepted(monkeypatch) -> None:
    _patch(monkeypatch, [_cues("同一个模型接不同工具效果差很多"), _cues("同一个模型接不同工具效果差很多")])
    text = asyncio.run(bl.bili_transcript("BV1btH36uEru", cfg=Cfg()))
    assert "效果差很多" in text


def test_mismatch_returns_empty(monkeypatch) -> None:
    # 三次三次内容，全都不一样 —— 就当没字幕
    _patch(
        monkeypatch,
        [_cues("今天讲珍妮古道尔的故事"), _cues("good good 我觉得我兴趣特别广泛"), _cues("锅包肉怎么做才好吃")],
    )
    assert asyncio.run(bl.bili_transcript("BV1btH36uEru", cfg=Cfg())) == ""


def test_no_subtitle_is_empty(monkeypatch) -> None:
    async def _get(path, params, *, timeout, cookies=None):
        if path.endswith("/view"):
            return {"bvid": "BV1btH36uEru", "cid": 1}
        return {"subtitle": {"subtitles": []}}

    monkeypatch.setattr(bl, "_get", _get)
    monkeypatch.setattr(bl, "_state_cookies", lambda cfg: {})
    assert asyncio.run(bl.bili_transcript("BV1btH36uEru", cfg=Cfg())) == ""
