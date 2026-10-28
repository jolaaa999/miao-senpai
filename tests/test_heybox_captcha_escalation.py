"""撞人机验证后的退避节奏。

背景（2026-10-27）：一天撞了 4 次验证，每次都按 3 小时退避，等于整天有一半在睡。
但实测同样一篇帖子，12:32 读是 0 字，16:12 再读就有 1993 字——验证是"闪一下"的。

所以退避分两档：
- 第一次撞：25 分钟后再来看；
- 连着第二次撞：才认为真被盯上，歇 3 小时；
- 只要成功读到一次，连击清零，下次又从 25 分钟开始。
"""

from __future__ import annotations

import asyncio

from dl_senpai import heybox as hs


def _reset(monkeypatch) -> None:
    monkeypatch.setattr(hs, "_captcha_streak", 0)
    monkeypatch.setattr(hs, "_read_cooldown_until", 0.0)


def test_first_hit_is_soft(monkeypatch) -> None:
    _reset(monkeypatch)
    assert hs.note_captcha() == hs._READ_COOLDOWN_SEC
    assert hs.captcha_streak() == 1


def test_second_consecutive_hit_escalates(monkeypatch) -> None:
    _reset(monkeypatch)
    hs.note_captcha()
    assert hs.note_captcha() == hs._READ_COOLDOWN_HARD_SEC
    assert hs.captcha_streak() == 2


def test_success_resets_streak(monkeypatch) -> None:
    _reset(monkeypatch)
    hs.note_captcha()
    hs.note_captcha()
    hs.note_success()
    assert hs.captcha_streak() == 0
    # 清零后下一次又是"第一次"，不该还按 3 小时罚
    assert hs.note_captcha() == hs._READ_COOLDOWN_SEC


def test_read_failure_sets_soft_cooldown(monkeypatch) -> None:
    _reset(monkeypatch)

    class _Bad:
        ok = False
        error = "empty_text"
        text = ""

    async def _read_link(url, cfg=None, focus=""):
        return _Bad()

    async def _gate(cfg=None, *, heavy=True):
        return None

    monkeypatch.setattr(hs, "_session_gate", _gate)
    import dl_senpai.link_reader as lr

    monkeypatch.setattr(lr, "read_link", _read_link)

    asyncio.run(hs.heybox_read("https://xiaoheihe.cn/app/bbs/link/aaaaaaaaaaaa"))
    remaining = hs.read_cooldown_remaining()
    assert hs._READ_COOLDOWN_SEC - 5 <= remaining <= hs._READ_COOLDOWN_SEC


def test_read_success_clears_streak(monkeypatch) -> None:
    _reset(monkeypatch)
    hs.note_captcha()

    class _Good:
        ok = True
        error = ""
        text = "正文" * 200

    async def _read_link(url, cfg=None, focus=""):
        return _Good()

    async def _gate(cfg=None, *, heavy=True):
        return None

    monkeypatch.setattr(hs, "_session_gate", _gate)
    import dl_senpai.link_reader as lr

    monkeypatch.setattr(lr, "read_link", _read_link)

    asyncio.run(hs.heybox_read("https://xiaoheihe.cn/app/bbs/link/aaaaaaaaaaaa"))
    assert hs.captcha_streak() == 0
