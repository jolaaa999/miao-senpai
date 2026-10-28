"""小黑盒的"出门间隔"分两档。

主人 2026-10-27 的判断：不是怕"多"，是怕"密"——一次只丢一个请求基本没事，
短时间连着丢一串就会回 show_captcha。

所以闸门分两档，这里钉住：
1. 重档（发评论、翻评论区）还是几分钟；
2. 轻档（读正文、看信息流）几十秒——不然群里说"看看这个帖子"她要等四分钟；
3. **读正文必须真的过闸门**：原来这条路一次都没登记，等于漏了口子。
"""

from __future__ import annotations

import asyncio

from dl_senpai import heybox as hs


class Cfg:
    heybox_session_gap_sec = 240
    heybox_request_gap_sec = 45


def test_two_tiers() -> None:
    assert hs._gate_gap(Cfg(), heavy=True) == 240
    assert hs._gate_gap(Cfg(), heavy=False) == 45


def test_light_tier_has_default() -> None:
    class Empty:
        heybox_session_gap_sec = 240

    # 没配也算得出来：轻档默认 45 秒
    assert hs._gate_gap(Empty(), heavy=False) == 45


def test_read_goes_through_gate(monkeypatch) -> None:
    calls: list[bool] = []

    async def _gate(cfg=None, *, heavy=True):
        calls.append(heavy)

    class _Doc:
        ok = True
        text = "正文" * 200
        error = ""

    async def _read_link(url, *, cfg=None, focus=""):
        return _Doc()

    monkeypatch.setattr(hs, "_session_gate", _gate)
    monkeypatch.setattr("dl_senpai.link_reader.read_link", _read_link)

    doc = asyncio.run(hs.heybox_read("249d72b2b8ac", cfg=Cfg()))
    assert doc.ok
    # 读正文走的是轻档
    assert calls == [False]
