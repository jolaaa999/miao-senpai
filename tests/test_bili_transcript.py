"""B站字幕：她看不了画面，字幕是她唯一能「知道视频讲了什么」的来源。

这层的坑都在细节里，所以测试盯这几条：
1. 人工字幕优先于 AI 字幕（AI 会把口播听成「投降 包村 拥 威信 扫」这种幻觉词）；
2. 逐句字幕要按 15 秒合并，否则一句一行、几千个时间戳把 token 全吃掉；
3. 没字幕要返回空串而不是报错（冷门/纯音乐/刚发布常常没有）；
4. 刷视频时必须把字幕塞进给模型的内容里——否则前面三条全白做。
"""

from __future__ import annotations

import asyncio
import json

from dl_senpai import bili as bl
from dl_senpai import bili_browse as bb


def test_prefers_manual_over_ai() -> None:
    rows = [
        {"lan": "ai-zh", "subtitle_url": "//ai.example/x.json"},
        {"lan": "zh-Hans", "subtitle_url": "//manual.example/y.json"},
    ]
    assert bl.pick_subtitle(rows)["lan"] == "zh-Hans"


def test_falls_back_to_ai() -> None:
    rows = [{"lan": "ai-zh", "subtitle_url": "//ai.example/x.json"}]
    assert bl.pick_subtitle(rows)["lan"] == "ai-zh"


def test_no_subtitle_returns_empty() -> None:
    assert bl.pick_subtitle([]) == {}
    assert bl.pick_subtitle([{"lan": "zh-Hans"}]) == {}  # 没 subtitle_url 等于没有


def test_cues_are_merged_into_15s_buckets() -> None:
    cues = [
        {"from": 0.0, "content": "第一句"},
        {"from": 3.0, "content": "第二句"},
        {"from": 20.0, "content": "第三句"},
    ]
    text = bl.transcript_from_cues(cues)
    lines = text.splitlines()
    assert len(lines) == 2, text
    assert lines[0].startswith("[0:00]") and "第一句" in lines[0] and "第二句" in lines[0]
    assert lines[1].startswith("[0:15]")


def test_transcript_is_truncated() -> None:
    cues = [{"from": float(i * 15), "content": "很长的一句话" * 6} for i in range(60)]
    text = bl.transcript_from_cues(cues, max_chars=300)
    assert len(text) < 420
    assert text.endswith("先到这儿）")


def test_no_subtitle_is_not_an_error(monkeypatch) -> None:
    async def _get(path, params, *, timeout, cookies=None):
        if path == "/x/web-interface/view":
            return {"bvid": "BV1", "cid": 42}
        return {"subtitle": {"subtitles": []}}

    monkeypatch.setattr(bl, "_get", _get)
    monkeypatch.setattr(bl, "_state_cookies", lambda cfg=None: {})
    # 必须用合法的 BV 号（BV + 10 位），否则 extract_bvid 直接判不认
    assert asyncio.run(bl.bili_transcript("BV1aFno6wETM", cfg=None)) == ""


def test_browse_feeds_transcript_to_model(tmp_path, monkeypatch) -> None:
    seen: list[str] = []

    class Cfg:
        bili_browse_enable = True
        bili_browse_comment_enable = False
        bili_browse_transcript_enable = True
        bili_transcript_max_chars = 2000
        bili_browse_diary_probability = 1.0
        bili_write_enable = False
        bili_browse_log_file = ""
        bili_actions_file = ""
        bili_memory_file = ""
        diary_enable = False
        own_life_quiet_start = 3
        own_life_quiet_end = 8

    class _Video:
        bvid = "BV1"
        title = "子弹时间怎么拍"
        owner = "某UP"
        desc = "简介"
        comments = []

    cfg = Cfg()
    cfg.bili_browse_log_file = str(tmp_path / "log.jsonl")
    cfg.bili_actions_file = str(tmp_path / "actions.json")
    cfg.bili_memory_file = str(tmp_path / "memo.json")

    async def _hot(limit=10, *, cfg=None):
        return [_Video()]

    async def _video(ref, *, comment_limit=5, cfg=None):
        return _Video()

    async def _transcript(ref, *, cfg=None, max_chars=2000):
        return "[0:00] 70 台手机围成一个球"

    async def _ask(c, instruction, data, *, llm=None):
        seen.append(data)
        return json.dumps({"pick": 1, "keep": False}, ensure_ascii=False)

    monkeypatch.setattr(bl, "bili_hot", _hot)
    monkeypatch.setattr(bl, "bili_video", _video)
    async def _no_like(ref, *, cfg=None, undo=False):
        return {"ok": True}

    async def _no_fav(ref, *, cfg=None, folder_id=0, undo=False):
        return {"ok": True, "folder_id": 1}

    monkeypatch.setattr(bl, "bili_like", _no_like)
    monkeypatch.setattr(bl, "bili_favorite", _no_fav)
    monkeypatch.setattr(bl, "bili_transcript", _transcript)
    monkeypatch.setattr("dl_senpai.ask_json.ask_json", _ask)
    monkeypatch.setattr("dl_senpai.heybox_browse._in_quiet_hours", lambda cfg, now=None: False)
    monkeypatch.setattr("dl_senpai.diary.get_diary_store", lambda d=None: None)

    record = asyncio.run(bb.browse_once(cfg, force=True))
    assert record.get("transcript_chars") == len("[0:00] 70 台手机围成一个球")
    joined = "\n".join(seen)
    assert "70 台手机围成一个球" in joined
    assert "别逐字引用" in joined
