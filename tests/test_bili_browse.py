"""B站自主刷：门禁 / 作息额度 / 只读（不碰平台写接口）的测试。

为什么钉这些：
1. "只读"是这个功能的安全前提。哪天有人在这儿加了点赞/评论调用，
   测试必须立刻红，而不是等封号才发现；
2. 日额度和静默期写错，就等于让它 24 小时不停地刷接口。
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from dl_senpai import bili as bl
from dl_senpai import bili_browse as bb


class Cfg:
    bili_browse_enable = True
    bili_browse_interval_min_sec = 5400
    bili_browse_interval_max_sec = 14400
    bili_browse_daily_max = 8
    bili_browse_diary_probability = 1.0
    bili_browse_log_file = ""
    bili_actions_file = ""
    bili_browse_schedule_file = ""
    diary_enable = True
    diary_dir = ""
    own_life_quiet_start = 3
    own_life_quiet_end = 8


class _Video:
    def __init__(self, bvid: str, title: str, owner: str = "某UP") -> None:
        self.bvid = bvid
        self.title = title
        self.owner = owner
        self.desc = "简介"
        self.comments: list[tuple[str, str, int]] = [("路人", "顶", 0)]


def _patch_env(monkeypatch, tmp_path, *, picks, notes=None, quiet=False):
    """把外部依赖全换成假的：热门口、视频详情、模型判断、日记本、静默判断。"""
    async def _feed(limit=12, *, cfg=None):
        # 每轮"刷新首页"的替身：返回同一批假视频，来源标成 rcmd
        return [_Video("BV1", "视频甲"), _Video("BV2", "视频乙")], "rcmd"

    monkeypatch.setattr(bl, "bili_feed", _feed)
    monkeypatch.setattr(bl, "bili_video", lambda ref, *, comment_limit=5, cfg=None: _async(_Video("BV1", "视频甲")))
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
    monkeypatch.setattr("dl_senpai.heybox_browse._in_quiet_hours", lambda cfg, now=None: quiet)

    answers = list(picks)

    async def _ask(cfg, instruction, data, *, llm=None):
        if answers:
            import json
            return json.dumps(answers.pop(0), ensure_ascii=False)
        return "{}"

    monkeypatch.setattr("dl_senpai.ask_json.ask_json", _ask)

    written: list[dict] = []

    class _Store:
        def add(self, entry):
            written.append(entry)

    monkeypatch.setattr("dl_senpai.diary.get_diary_store", lambda d=None: _Store())
    return written


def _async(value):
    async def _inner():
        return value

    return _inner()


def test_disabled_skips(monkeypatch, tmp_path) -> None:
    cfg = Cfg()
    cfg.bili_browse_enable = False
    cfg.bili_browse_log_file = str(tmp_path / "log.jsonl")
    cfg.bili_actions_file = str(tmp_path / "actions.json")
    record = asyncio.run(bb.browse_once(cfg))
    assert record["skipped"] == "bili_browse_disabled"


def test_quiet_hours_skips(monkeypatch, tmp_path) -> None:
    cfg = Cfg()
    cfg.bili_browse_log_file = str(tmp_path / "log.jsonl")
    cfg.bili_actions_file = str(tmp_path / "actions.json")
    _patch_env(monkeypatch, tmp_path, picks=[{"pick": 1}], quiet=True)
    record = asyncio.run(bb.browse_once(cfg))
    assert record["skipped"] == "quiet_hours"


def test_no_interest_does_not_read(monkeypatch, tmp_path) -> None:
    cfg = Cfg()
    cfg.bili_browse_log_file = str(tmp_path / "log.jsonl")
    cfg.bili_actions_file = str(tmp_path / "actions.json")
    _patch_env(monkeypatch, tmp_path, picks=[{"pick": 0, "why": "都不想看"}])
    record = asyncio.run(bb.browse_once(cfg))
    assert record["skipped"] == "no_interest"
    assert "bvid" not in record


def test_dice_can_skip_note(monkeypatch, tmp_path) -> None:
    cfg = Cfg()
    cfg.bili_browse_diary_probability = 0.0
    cfg.bili_browse_log_file = str(tmp_path / "log.jsonl")
    cfg.bili_actions_file = str(tmp_path / "actions.json")
    written = _patch_env(monkeypatch, tmp_path, picks=[{"pick": 1}])
    record = asyncio.run(bb.browse_once(cfg))
    assert record["skipped"] == "dice"
    assert written == []


def test_writes_diary_only(monkeypatch, tmp_path) -> None:
    cfg = Cfg()
    cfg.bili_browse_log_file = str(tmp_path / "log.jsonl")
    cfg.bili_actions_file = str(tmp_path / "actions.json")
    written = _patch_env(
        monkeypatch,
        tmp_path,
        picks=[{"pick": 1, "why": "想看看"}, {"keep": True, "note": "今天刷到个讲买车的"}],
    )

    # 只读的前提：任何写平台的调用都应该没被碰过
    def _boom(*args, **kwargs):
        raise AssertionError("自主刷B站不许调用平台写接口")

    monkeypatch.setattr(bl, "bili_comment", _boom)

    record = asyncio.run(bb.browse_once(cfg))
    assert record["keep"] is True
    assert record["diary"] == "今天刷到个讲买车的"
    assert written and written[0]["text"] == "今天刷到个讲买车的"
    assert written[0]["tags"] == ["B站"]


def test_nothing_to_say_writes_nothing(monkeypatch, tmp_path) -> None:
    cfg = Cfg()
    cfg.bili_browse_log_file = str(tmp_path / "log.jsonl")
    cfg.bili_actions_file = str(tmp_path / "actions.json")
    written = _patch_env(
        monkeypatch, tmp_path, picks=[{"pick": 1}, {"keep": False, "note": ""}]
    )
    record = asyncio.run(bb.browse_once(cfg))
    assert record["skipped"] == "nothing_to_say"
    assert written == []


def test_daily_count_resets_next_day(tmp_path, monkeypatch) -> None:
    cfg = Cfg()
    cfg.bili_browse_schedule_file = str(tmp_path / "sched.json")
    assert bb._count_today(cfg) == 0
    assert bb._bump_today(cfg) == 1
    assert bb._bump_today(cfg) == 2
    assert bb._count_today(cfg) == 2

    # 「明天」按今天算，别硬编码日期：写死 10-27 时，真实日期走到那天
    # 本测试会挂（写入的计数 key 就是同一天）。先把原类存下来，
    # 否则类体里的 bb.datetime 已经被替换成 _Tomorrow 自己。
    _real_dt = bb.datetime

    class _Tomorrow(_real_dt):
        @classmethod
        def now(cls, tz=None):  # noqa: D102
            return _real_dt.now() + timedelta(days=1)

    monkeypatch.setattr(bb, "datetime", _Tomorrow)
    assert bb._count_today(cfg) == 0


def test_gap_within_bounds(monkeypatch) -> None:
    cfg = Cfg()

    class _Noon(bb.datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102
            return bb.datetime(2026, 10, 26, 15, 0)

    monkeypatch.setattr(bb, "datetime", _Noon)
    for _ in range(30):
        gap = bb._next_gap_seconds(cfg)
        assert 60 <= gap <= 6 * 3600


def _cfg_with_tmp(tmp_path):
    cfg = Cfg()
    cfg.bili_browse_log_file = str(tmp_path / 'log.jsonl')
    cfg.bili_browse_schedule_file = str(tmp_path / 'sched.json')
    cfg.bili_actions_file = str(tmp_path / 'actions.json')
    cfg.diary_dir = str(tmp_path / 'diary')
    return cfg


def test_recently_seen_video_is_filtered(tmp_path, monkeypatch) -> None:
    """2026-10-27 实况：同一个 BV 一天被挑中 7 次，日记堆了四条一样的话。"""
    import json
    import time

    _patch_env(monkeypatch, tmp_path, picks=[{'pick': 1, 'why': '还想看'}])
    cfg = _cfg_with_tmp(tmp_path)
    (tmp_path / 'log.jsonl').write_text(
        json.dumps({'ts': time.time(), 'bvid': 'BV1', 'title': '视频甲'}) + '\n',
        encoding='utf-8',
    )
    record = asyncio.run(bb.browse_once(cfg, force=True))
    assert record.get('dedupe') == 1
    # 剔掉 BV1 之后，1 号位变成 BV2
    assert record.get('bvid') == 'BV2'


def test_all_seen_skips_instead_of_replaying(tmp_path, monkeypatch) -> None:
    import json
    import time

    _patch_env(monkeypatch, tmp_path, picks=[{'pick': 1}])
    cfg = _cfg_with_tmp(tmp_path)
    rows = [{'ts': time.time(), 'bvid': b, 'title': 'x'} for b in ('BV1', 'BV2')]
    (tmp_path / 'log.jsonl').write_text(
        '\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n',
        encoding='utf-8',
    )
    record = asyncio.run(bb.browse_once(cfg, force=True))
    assert record.get('skipped') == 'all_seen'
