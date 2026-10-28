"""B站记忆本：去重 / 上限 / 检索 / 与自主刷的接线。

为什么钉这些：
1. 上限写错会无限长（她天天刷，一年几千条）；
2. 去重写错就变成"同一条视频记十遍"，本子立刻没价值；
3. 刷完却没真写进本子，是这类功能最常见的静默失败。
"""

from __future__ import annotations

import asyncio
import json

from dl_senpai import bili as bl
from dl_senpai import bili_browse as bb
from dl_senpai import bili_memory as bm


class Cfg:
    bili_memory_file = ""
    bili_memory_max = 200
    bili_browse_enable = True
    bili_browse_diary_probability = 1.0
    bili_browse_log_file = ""
    bili_actions_file = ""
    diary_enable = True
    diary_dir = ""
    own_life_quiet_start = 3
    own_life_quiet_end = 8


def _cfg(tmp_path) -> Cfg:
    cfg = Cfg()
    cfg.bili_memory_file = str(tmp_path / "memo.json")
    cfg.bili_browse_log_file = str(tmp_path / "log.jsonl")
    cfg.bili_actions_file = str(tmp_path / "actions.json")
    return cfg


def test_remember_then_load(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    memo, why = bm.remember(cfg, bvid="BV1", title="讲买车", note="买车先看保值率", topic="买车")
    assert memo is not None and why == ""
    rows = bm.load(cfg)
    assert len(rows) == 1 and rows[0].note == "买车先看保值率"


def test_same_video_not_stored_twice(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    bm.remember(cfg, bvid="BV1", title="甲", note="第一句话")
    memo, why = bm.remember(cfg, bvid="BV1", title="甲", note="完全不同的话")
    assert memo is None and "记过" in why
    assert len(bm.load(cfg)) == 1


def test_similar_note_is_skipped(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    bm.remember(cfg, bvid="BV1", title="甲", note="买车先看保值率和保养成本")
    memo, why = bm.remember(cfg, bvid="BV2", title="乙", note="买车先看保值率和保养成本啊")
    assert memo is None and "太像" in why


def test_cap_drops_oldest(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    cfg.bili_memory_max = 3
    for i in range(5):
        bm.remember(cfg, bvid=f"BV{i}", title=f"第{i}条", note=f"第{i}条内容不一样{i*7}")
    rows = bm.load(cfg)
    assert len(rows) == 3
    assert rows[-1].bvid == "BV4"


def test_search_by_keyword_and_recent(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    bm.remember(cfg, bvid="BV1", title="甲", note="显卡降价了", topic="显卡")
    bm.remember(cfg, bvid="BV2", title="乙", note="买车先看保值率", topic="买车")
    assert [r.bvid for r in bm.search(cfg, "买车")] == ["BV2"]
    assert [r.bvid for r in bm.search(cfg, "")] == ["BV2", "BV1"]


def test_render_empty() -> None:
    assert "还没记住" in bm.render([])


def test_bilimemo_marker_forms() -> None:
    from dl_senpai.agent.tools.social_tools import _parse_bilimemo

    _, calls = _parse_bilimemo("<<<BILIMEMO>>>")
    assert calls[0].arguments == {"action": "recent", "limit": 6}

    _, calls = _parse_bilimemo("<<<BILIMEMO 3>>>")
    assert calls[0].arguments["limit"] == 3

    _, calls = _parse_bilimemo("<<<BILIMEMO 买车>>>")
    assert calls[0].arguments == {"action": "search", "query": "买车", "limit": 6}


def test_browse_writes_memory_when_asked(tmp_path, monkeypatch) -> None:
    """自主刷的那条链路：模型说值得记，本子里就得真有。"""
    cfg = _cfg(tmp_path)

    class _Video:
        bvid = "BV9"
        title = "如何买人生第一辆车"
        owner = "某UP"
        desc = "简介"
        comments = [("路人", "顶", 0)]

    async def _feed(limit=12, *, cfg=None):
        # browse_once 走的是 bili_feed（返回 (视频列表, 来源)）。
        # 只 mock bili_hot 等于没 mock，测试会去打真实接口——
        # 这个用例之前就是因此红的：本子里记进来的是真实刷到的视频，
        # 断言里写的却是 BV9。
        return [_Video()], "hot"

    def _no_network(*args, **kwargs):
        raise AssertionError("测试里不许打真实 B 站接口")

    async def _video(ref, *, comment_limit=5, cfg=None):
        return _Video()

    answers = [
        {"pick": 1, "why": "想看"},
        {"keep": True, "note": "等真要买车再翻出来", "remember": True, "topic": "买车"},
    ]

    async def _ask(c, instruction, data, *, llm=None):
        return json.dumps(answers.pop(0), ensure_ascii=False)

    written: list[dict] = []

    class _Store:
        def add(self, entry):
            written.append(entry)

    monkeypatch.setattr(bl, "bili_feed", _feed)
    monkeypatch.setattr(bl, "bili_hot", _no_network)
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
    monkeypatch.setattr("dl_senpai.ask_json.ask_json", _ask)
    monkeypatch.setattr("dl_senpai.diary.get_diary_store", lambda d=None: _Store())
    monkeypatch.setattr("dl_senpai.heybox_browse._in_quiet_hours", lambda cfg, now=None: False)

    def _boom(*args, **kwargs):
        raise AssertionError("自主刷B站不许调用平台写接口")

    monkeypatch.setattr(bl, "bili_comment", _boom)

    record = asyncio.run(bb.browse_once(cfg))
    assert record.get("diary")
    assert record.get("memo") == "等真要买车再翻出来"
    rows = bm.load(cfg)
    assert len(rows) == 1
    assert rows[0].bvid == "BV9" and rows[0].topic == "买车"
    assert written and written[0]["tags"] == ["B站"]
