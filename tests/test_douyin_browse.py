"""抖音自主刷的测试。

盯四件事：
1. pick 0 表示"一个都不想看"——这是唯一允许不刷的出口，不能被当成失败；
2. 刷完必须落日记（不然她白刷了）和可选的记忆；
3. 当日上限要真的挡住；
4. **只读**：这一层不许有任何点赞/收藏/评论的调用（module 里不该有这些东西）。
"""

from __future__ import annotations

import asyncio
import json

from dl_senpai import douyin as dy
from dl_senpai import douyin_browse as db


class Cfg:
    douyin_enable = True
    douyin_daily_limit = 12
    douyin_browse_diary_probability = 1.0
    douyin_browse_log_file = ""
    douyin_browse_schedule_file = ""
    douyin_memory_file = ""
    douyin_memory_max = 200
    diary_enable = True
    own_life_quiet_start = 3
    own_life_quiet_end = 8


class _Store:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, entry) -> None:  # noqa: ANN001
        self.rows.append(dict(entry))


def _setup(tmp_path, monkeypatch, *, answers, comments=3):
    cfg = Cfg()
    cfg.douyin_browse_log_file = str(tmp_path / "log.jsonl")
    cfg.douyin_browse_schedule_file = str(tmp_path / "sched.json")
    cfg.douyin_memory_file = str(tmp_path / "memo.json")

    videos = [
        dy.DouyinVideo(aweme_id="7001", desc="钓鱼佬今天爆护了", author="老王", digg=12000),
        dy.DouyinVideo(aweme_id="7002", desc="这条鱼比我手臂还粗", author="小李", digg=300),
    ]

    async def _feed(*, cfg=None, limit=12):
        return videos

    async def _video(aweme_id, *, cfg=None, comment_limit=5):
        return dy.DouyinVideo(
            aweme_id=aweme_id,
            desc="钓鱼佬今天爆护了",
            author="老王",
            digg=12000,
            comment_count=88,
            comments=[("路人甲", "这抄网真大", 12)][:comments],
        )

    queue = list(answers)

    async def _ask(c, instruction, data, *, llm=None):
        return json.dumps(queue.pop(0) if queue else {}, ensure_ascii=False)

    store = _Store()
    monkeypatch.setattr(dy, "dy_feed", _feed)
    monkeypatch.setattr(dy, "dy_video", _video)
    # douyin_browse 在模块顶部就 import 了 ask_json，所以要打在模块自己的名字上
    monkeypatch.setattr(db, "ask_json", _ask)
    monkeypatch.setattr("dl_senpai.heybox_browse._in_quiet_hours", lambda cfg, now=None: False)
    monkeypatch.setattr("dl_senpai.diary.get_diary_store", lambda d=None: store)
    return cfg, store


def test_browse_writes_diary(tmp_path, monkeypatch) -> None:
    cfg, store = _setup(
        tmp_path,
        monkeypatch,
        answers=[{"pick": 1, "why": "钓鱼"}, {"keep": True, "note": "今天又刷到钓鱼的"}],
    )
    record = asyncio.run(db.browse_once(cfg, force=True))
    assert record.get("diary") == "今天又刷到钓鱼的"
    assert store.rows and store.rows[0]["tags"] == ["抖音"]
    log = [
        json.loads(line)
        for line in (tmp_path / "log.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert log[-1]["aweme_id"] == "7001"


def test_pick_zero_means_nothing_interesting(tmp_path, monkeypatch) -> None:
    cfg, store = _setup(tmp_path, monkeypatch, answers=[{"pick": 0}])
    record = asyncio.run(db.browse_once(cfg, force=True))
    assert record.get("skipped") == "no_interest"
    assert store.rows == []


def test_remember_goes_to_douyin_memory(tmp_path, monkeypatch) -> None:
    cfg, _store = _setup(
        tmp_path,
        monkeypatch,
        answers=[
            {"pick": 2},
            {"keep": True, "note": "抄网要买大的", "remember": True, "topic": "钓鱼"},
        ],
    )
    record = asyncio.run(db.browse_once(cfg, force=True))
    assert record.get("memo") == "抄网要买大的"
    saved = json.loads((tmp_path / "memo.json").read_text(encoding="utf-8"))
    assert saved["items"][0]["bvid"] == "dy:7002"


def test_daily_cap_blocks(tmp_path, monkeypatch) -> None:
    cfg, store = _setup(tmp_path, monkeypatch, answers=[{"pick": 1}])
    cfg.douyin_daily_limit = 1
    (tmp_path / "sched.json").write_text(
        json.dumps({"day": db.datetime.now().date().isoformat(), "count": 1}), encoding="utf-8"
    )
    record = asyncio.run(db.browse_once(cfg))
    assert record.get("skipped") == "daily_cap"


def test_module_is_read_only() -> None:
    # 只读是这一层的承诺：有写操作就说明有人偷偷加了
    for name in ("like", "favorite", "comment", "publish"):
        assert not hasattr(db, name)
