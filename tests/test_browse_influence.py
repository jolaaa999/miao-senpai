"""刷到的东西要「影响她」，而不是只躺在文件里。

这层的价值全在「真的进了每轮提示词」这一件事上，所以测试盯三点：
1. 时间窗之外的旧东西不许再冒出来（否则她天天回味上周的帖子）；
2. 开关关掉时一个字都不许多嘴；
3. build_diary_brief 必须把它拼进去——拼不进去等于白做，
   而且这件事只能靠测试发现（线上表现只是「她好像没记得」）。
"""

from __future__ import annotations

import json
import time

from dl_senpai import browse_influence as bi
from dl_senpai.diary import DiaryStore, build_diary_brief


class Cfg:
    browse_influence_enable = True
    browse_influence_hours = 48
    browse_influence_max_items = 4
    heybox_browse_log_file = ""
    # 抖音台账也要指到 tmp：不指的话这层会读到线上真实数据，断言随线上漂移
    douyin_browse_log_file = ""
    bili_memory_file = ""
    diary_context_n = 3


def _cfg(tmp_path) -> Cfg:
    cfg = Cfg()
    cfg.heybox_browse_log_file = str(tmp_path / "heybox.jsonl")
    cfg.douyin_browse_log_file = str(tmp_path / "douyin.jsonl")
    cfg.bili_memory_file = str(tmp_path / "memo.json")
    return cfg


def _write_heybox(cfg, rows) -> None:
    from pathlib import Path

    Path(cfg.heybox_browse_log_file).write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )


def test_collects_recent_from_both_platforms(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    from dl_senpai import bili_memory as bm

    bm.remember(cfg, bvid="BV1", title="子弹时间", note="想弄明白怎么同步拍", topic="子弹时间")
    now = time.time()
    _write_heybox(
        cfg,
        [
            {"ts": now - 600, "title": "钓到一条大肥鱼", "read_chars": 3000},
            {"ts": now - 300, "title": "养鱼翻车", "commented": "这鱼真大"},
        ],
    )
    items = bi.recent_items(cfg, hours=48, limit=6)
    joined = " | ".join(items)
    assert "子弹时间" in joined
    assert "钓到一条大肥鱼" in joined
    assert "这鱼真大" in joined


def test_old_items_are_dropped(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    _write_heybox(cfg, [{"ts": time.time() - 10 * 24 * 3600, "title": "上周的旧帖", "read_chars": 900}])
    assert bi.recent_items(cfg, hours=48) == []


def test_limit_is_respected(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    now = time.time()
    _write_heybox(
        cfg,
        [{"ts": now - i, "title": f"帖子{i}", "read_chars": 100} for i in range(8)],
    )
    assert len(bi.recent_items(cfg, hours=48, limit=3)) == 3


def test_disabled_returns_empty(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    _write_heybox(cfg, [{"ts": time.time(), "title": "有帖子", "read_chars": 100}])
    cfg.browse_influence_enable = False
    assert bi.build_browse_brief(cfg) == ""


def test_brief_has_no_read_aloud_invite(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    _write_heybox(cfg, [{"ts": time.time(), "title": "有帖子", "read_chars": 100}])
    brief = bi.build_browse_brief(cfg)
    assert brief.startswith("【我最近自己刷到的】")
    assert "别把这段原文念出来" in brief


def test_diary_brief_includes_influence(tmp_path, monkeypatch) -> None:
    cfg = _cfg(tmp_path)
    store = DiaryStore(tmp_path / "diary")
    store.add("今天钓了条大鱼")
    monkeypatch.setattr("dl_senpai.config.get_config", lambda: cfg)
    monkeypatch.setattr(
        "dl_senpai.browse_influence.build_browse_brief", lambda c: "【我最近自己刷到的】MARK"
    )
    brief = build_diary_brief(store, n=3)
    assert "MARK" in brief
    assert "今天钓了条大鱼" in brief
