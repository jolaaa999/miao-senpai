"""小黑盒「刷了就评论」的测试。

主人定的口味是：只要她刷帖子（读完了正文），就该在评论区说一句。
所以这里钉两件事：
1. 骰子关掉（p=1.0）时，不许再因为"概率没过"而跳过；
2. 模型即使把 comment 判成 false，只要它给了文案，也照样发出去——
   一票否决权被收回去了，不然「刷了就评」只是句口号。
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
    text = "正文" * 200


class _Sent:
    def __init__(self, text: str) -> None:
        self.text = text
        self.error = ""


def _setup(monkeypatch, tmp_path, *, answer, sent: list[str]):
    cfg = Cfg()
    cfg.heybox_browse_log_file = str(tmp_path / "log.jsonl")
    cfg.heybox_write_state_file = str(tmp_path / "state.json")

    async def _feed(*, cfg=None, limit=12):
        return [_Post()]

    async def _read(ref, *, cfg=None, limit=200):
        return _Doc()

    async def _comment(ref, text, *, cfg=None, dry_run=False):
        sent.append(text)
        return _Sent(text)

    async def _ask(c, instruction, data, *, llm=None):
        return json.dumps(answer, ensure_ascii=False)

    monkeypatch.setattr(hs, "heybox_feed", _feed)
    monkeypatch.setattr(hs, "heybox_read", _read)
    monkeypatch.setattr(hs, "heybox_comment", _comment)
    monkeypatch.setattr(hb, "_ask", _ask)
    monkeypatch.setattr("dl_senpai.write_guard.check_can_comment", lambda *a, **k: "")
    monkeypatch.setattr("dl_senpai.write_guard.record_comment", lambda *a, **k: None)
    return cfg


def test_probability_one_never_skips_on_dice(tmp_path, monkeypatch) -> None:
    sent: list[str] = []
    cfg = _setup(
        monkeypatch,
        tmp_path,
        answer={"pick": 1},
        sent=sent,
    )
    record = asyncio.run(hb.browse_once(cfg, force=True))
    # 第一个判断是挑帖，第二个（这里没给）会退回 {}：文案为空 -> nothing_to_say，
    # 但绝不能是 dice 跳过
    assert record.get("skipped") != "dice"


def test_posts_even_when_model_says_false(tmp_path, monkeypatch) -> None:
    sent: list[str] = []
    answers = iter([
        {"pick": 1, "why": "有梗"},
        {"comment": False, "text": "这鱼看着比我上次钓的还大"},
    ])

    async def _ask(c, instruction, data, *, llm=None):
        try:
            return json.dumps(next(answers), ensure_ascii=False)
        except StopIteration:
            return "{}"

    cfg = _setup(monkeypatch, tmp_path, answer={"pick": 1}, sent=sent)
    monkeypatch.setattr(hb, "_ask", _ask)

    record = asyncio.run(hb.browse_once(cfg, force=True))
    assert record.get("commented") == "这鱼看着比我上次钓的还大"
    assert sent == ["这鱼看着比我上次钓的还大"]


def test_empty_text_still_skips(tmp_path, monkeypatch) -> None:
    sent: list[str] = []
    answers = iter([{"pick": 1}, {"comment": False, "text": ""}])

    async def _ask(c, instruction, data, *, llm=None):
        try:
            return json.dumps(next(answers), ensure_ascii=False)
        except StopIteration:
            return "{}"

    cfg = _setup(monkeypatch, tmp_path, answer={"pick": 1}, sent=sent)
    monkeypatch.setattr(hb, "_ask", _ask)

    record = asyncio.run(hb.browse_once(cfg, force=True))
    assert record.get("skipped") == "nothing_to_say"
    assert sent == []
