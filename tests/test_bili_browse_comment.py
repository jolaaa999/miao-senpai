"""B站「刷了就评论」的测试：评论不受日记骰子影响。

主人要的是：刷到一个视频，就在它下面说一句。所以这里钉三件事：
1. 写开关关着时，不去尝试发（省一次无意义的请求）；
2. 写开关打开后，**即使日记骰子是 0**，评论照样发；
3. 发失败（比如号还没到 Lv2）要把原因记进台账，不能静默当成功。
"""

from __future__ import annotations

import asyncio
import json

from dl_senpai import bili as bl
from dl_senpai import bili_browse as bb


class Cfg:
    bili_browse_enable = True
    bili_browse_comment_enable = True
    bili_browse_diary_probability = 0.0
    bili_write_enable = True
    bili_browse_log_file = ""
    bili_actions_file = ""
    bili_memory_file = ""
    bili_write_state_file = ""
    bili_comment_min_gap_sec = 0
    diary_enable = False
    own_life_quiet_start = 3
    own_life_quiet_end = 8


class _Video:
    bvid = "BV1test"
    title = "如何买人生第一辆车"
    owner = "某UP"
    desc = "简介"
    comments = [("路人", "顶", 0)]


def _setup(tmp_path, monkeypatch, *, answers, sent: list[str], fail: str = ""):
    cfg = Cfg()
    cfg.bili_browse_log_file = str(tmp_path / "log.jsonl")
    cfg.bili_actions_file = str(tmp_path / "actions.json")
    cfg.bili_memory_file = str(tmp_path / "memo.json")
    cfg.bili_write_state_file = str(tmp_path / "state.json")

    async def _hot(limit=10, *, cfg=None):
        return [_Video()]

    async def _video(ref, *, comment_limit=5, cfg=None):
        return _Video()

    async def _comment(ref, text, *, cfg=None, root_id=0, parent_id=0):
        if fail:
            raise bl.BiliError(fail)
        sent.append(text)
        return {"ok": True, "rpid": 123}

    queue = list(answers)

    async def _ask(c, instruction, data, *, llm=None):
        return json.dumps(queue.pop(0) if queue else {}, ensure_ascii=False)

    monkeypatch.setattr(bl, "bili_hot", _hot)
    monkeypatch.setattr(bl, "bili_video", _video)
    async def _no_transcript(ref, *, cfg=None, max_chars=2000):
        # 这些用例不测字幕，别让它去打真网络
        return ""

    monkeypatch.setattr(bl, "bili_transcript", _no_transcript)
    async def _no_like(ref, *, cfg=None, undo=False):
        # 点赞/收藏是真实写操作，用例里一律别打出去
        return {"ok": True}

    async def _no_fav(ref, *, cfg=None, folder_id=0, undo=False):
        return {"ok": True, "folder_id": 1}

    monkeypatch.setattr(bl, "bili_like", _no_like)
    monkeypatch.setattr(bl, "bili_favorite", _no_fav)
    monkeypatch.setattr(bl, "bili_comment", _comment)
    monkeypatch.setattr("dl_senpai.ask_json.ask_json", _ask)
    monkeypatch.setattr("dl_senpai.heybox_browse._in_quiet_hours", lambda cfg, now=None: False)
    monkeypatch.setattr("dl_senpai.write_guard.check_can_comment", lambda *a, **k: "")
    monkeypatch.setattr("dl_senpai.write_guard.record_comment", lambda *a, **k: None)
    return cfg


def test_comment_even_when_diary_dice_is_zero(tmp_path, monkeypatch) -> None:
    sent: list[str] = []
    cfg = _setup(
        tmp_path,
        monkeypatch,
        answers=[{"pick": 1}, {"text": "这车我也在攒钱买"}],
        sent=sent,
    )
    record = asyncio.run(bb.browse_once(cfg, force=True))
    assert record.get("commented") == "这车我也在攒钱买"
    assert sent == ["这车我也在攒钱买"]
    # 日记骰子是 0，所以日记那步会被跳过——正好证明评论不依赖它
    assert record.get("skipped") == "dice"


def test_no_attempt_when_write_disabled(tmp_path, monkeypatch) -> None:
    sent: list[str] = []
    cfg = _setup(tmp_path, monkeypatch, answers=[{"pick": 1}], sent=sent)
    cfg.bili_write_enable = False
    record = asyncio.run(bb.browse_once(cfg, force=True))
    assert record.get("comment_skipped") == "write_disabled"
    assert sent == []


def test_comment_failure_is_recorded(tmp_path, monkeypatch) -> None:
    sent: list[str] = []
    cfg = _setup(
        tmp_path,
        monkeypatch,
        answers=[{"pick": 1}, {"text": "说一句"}],
        sent=sent,
        fail="B站没接受这条评论：code=12074 B站要求 Lv2 才能评论。这号现在是 Lv1，得再攒经验升一级",
    )
    record = asyncio.run(bb.browse_once(cfg, force=True))
    assert "comment_error" in record
    assert "12074" in record["comment_error"]
    assert sent == []
