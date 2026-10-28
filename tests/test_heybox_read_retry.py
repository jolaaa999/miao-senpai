"""小黑盒读正文的重试与风控退避。

背景（2026-10-27 实测）：12:32~15:03 连续 4 次 tick 全部 read_chars=0，
16:12 再读同一篇立刻拿到 1993 字。也就是说风控是"闪一下"的，
一次失败不代表被封。所以：

1. 第一次拿不到字要重试一次，别直接放弃（那 4 次本来有 3 次可以救回来）；
2. 连着两次都拿不到，就认定被风控，标记 blocked 交给循环退避——
   继续按 2 小时节奏硬撞只会把验证撞得更死。
"""

from __future__ import annotations

import asyncio
import json

from dl_senpai import heybox as hs
from dl_senpai import heybox_browse as hb


class Cfg:
    heybox_browse_enable = True
    heybox_browse_comment_probability = 1.0
    heybox_browse_reply_enable = False
    heybox_browse_log_file = ""
    heybox_write_state_file = ""
    heybox_comment_min_gap_sec = 0
    own_life_quiet_start = 3
    own_life_quiet_end = 8
    diary_enable = False


class _Post:
    link_id = "abc123"
    title = "今天钓到一条大肥鱼"
    topics = "钓鱼"
    comments = 3
    desc = "摘要"


class _Doc:
    def __init__(self, text: str) -> None:
        self.text = text


class _Sent:
    def __init__(self, text: str) -> None:
        self.text = text
        self.error = ""


def _setup(monkeypatch, tmp_path, *, answers, reads):
    """reads 是每次 heybox_read 依次返回的正文（空串=这次被风控）。"""
    cfg = Cfg()
    cfg.heybox_browse_log_file = str(tmp_path / "log.jsonl")
    cfg.heybox_write_state_file = str(tmp_path / "state.json")

    sent: list[str] = []
    queue = list(reads)

    async def _feed(*, cfg=None, limit=12):
        return [_Post()]

    async def _read(ref, *, cfg=None, limit=200):
        text = queue.pop(0) if queue else ""
        if not text:
            raise RuntimeError("页面没渲染出来")
        return _Doc(text)

    async def _comment(ref, text, *, cfg=None, dry_run=False):
        sent.append(text)
        return _Sent(text)

    answer_queue = list(answers)

    async def _ask(c, instruction, data, *, llm=None):
        return json.dumps(answer_queue.pop(0) if answer_queue else {}, ensure_ascii=False)

    monkeypatch.setattr(hs, "heybox_feed", _feed)
    monkeypatch.setattr(hs, "heybox_read", _read)
    monkeypatch.setattr(hs, "heybox_comment", _comment)
    monkeypatch.setattr(hb, "_ask", _ask)
    monkeypatch.setattr("dl_senpai.write_guard.check_can_comment", lambda *a, **k: "")
    monkeypatch.setattr("dl_senpai.write_guard.record_comment", lambda *a, **k: None)
    return cfg, sent


def test_second_attempt_saves_the_tick(tmp_path, monkeypatch) -> None:
    cfg, sent = _setup(
        monkeypatch,
        tmp_path,
        answers=[{"pick": 1}, {"text": "这鱼得有十斤吧"}],
        reads=["", "正文" * 200],
    )
    record = asyncio.run(hb.browse_once(cfg, force=True))
    assert record.get("read_chars", 0) >= 200
    assert not record.get("blocked")
    assert sent == ["这鱼得有十斤吧"]


def test_two_empty_reads_mark_blocked(tmp_path, monkeypatch) -> None:
    cfg, sent = _setup(
        monkeypatch,
        tmp_path,
        answers=[{"pick": 1}, {"text": "不该发出去"}],
        reads=["", ""],
    )
    record = asyncio.run(hb.browse_once(cfg, force=True))
    assert record.get("skipped") == "empty_text"
    # blocked 是给循环看的信号：别按原节奏继续撞
    assert record.get("blocked") is True
    assert sent == []
