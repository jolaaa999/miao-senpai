"""评论节奏护栏 + 刷帖子流的测试。

为什么钉这些：日限放开之后，"别看起来像脚本"全靠这几条软约束。
它们坏掉不会报错，只会让她悄悄变成评论机。
"""

from __future__ import annotations

import time

import pytest

from dl_senpai import heybox as hs
from dl_senpai.write_guard import check_can_comment, load_state, record_comment


class _Cfg:
    def __init__(self, tmp: str) -> None:
        self.heybox_write_state_file = tmp
        self.heybox_comment_min_gap_sec = 600


def test_first_comment_allowed(tmp_path) -> None:
    cfg = _Cfg(str(tmp_path / "state.json"))
    assert check_can_comment("abc123", "这波我也遇到过", cfg) == ""


def test_gap_blocks_second_comment(tmp_path) -> None:
    cfg = _Cfg(str(tmp_path / "state.json"))
    record_comment("abc123", "这波我也遇到过", cfg)
    assert "隔" in check_can_comment("def456", "另一个帖子的话", cfg)


def test_same_link_blocked(tmp_path) -> None:
    cfg = _Cfg(str(tmp_path / "state.json"))
    record_comment("abc123", "这波我也遇到过", cfg)
    cfg.heybox_comment_min_gap_sec = 0
    assert "评过" in check_can_comment("abc123", "换句话试试", cfg)


def test_similar_text_blocked(tmp_path) -> None:
    cfg = _Cfg(str(tmp_path / "state.json"))
    cfg.heybox_comment_min_gap_sec = 0
    record_comment("abc123", "这套配装我试过，斩杀确实稳", cfg)
    assert "太像" in check_can_comment("def456", "这套配装我试过，斩杀确实很稳", cfg)


def test_different_text_allowed(tmp_path) -> None:
    cfg = _Cfg(str(tmp_path / "state.json"))
    cfg.heybox_comment_min_gap_sec = 0
    record_comment("abc123", "这套配装我试过，斩杀确实稳", cfg)
    assert check_can_comment("def456", "昨晚更新完帧数掉得离谱", cfg) == ""


def test_state_file_shape(tmp_path) -> None:
    cfg = _Cfg(str(tmp_path / "state.json"))
    record_comment("abc123", "内容", cfg, title="标题")
    state = load_state(cfg)
    assert state["comments"][-1]["link_id"] == "abc123"
    assert state["comments"][-1]["title"] == "标题"
    assert state["last_comment_ts"] > 0


def test_feed_render_marks_read_marker() -> None:
    posts = [
        hs.FeedPost(link_id="aaaa1111", title="标题一", desc="摘要", comments=12, topics="Steam"),
        hs.FeedPost(link_id="bbbb2222", title="标题二", desc="", comments=0, topics=""),
    ]
    text = hs.render_feed(posts, limit=5)
    assert "<<<HEYBOX read aaaa1111>>>" in text
    assert "<<<HEYBOX read bbbb2222>>>" in text
    assert "12 评" in text


def test_feed_render_limit_note() -> None:
    posts = [hs.FeedPost(link_id=f"id{i}", title=f"t{i}") for i in range(6)]
    text = hs.render_feed(posts, limit=3)
    assert "还有 3 条没列" in text


def test_quiet_hours_night() -> None:
    from datetime import datetime

    from dl_senpai.heybox_browse import _in_quiet_hours

    class Cfg:
        own_life_quiet_start = 3
        own_life_quiet_end = 8

    assert _in_quiet_hours(Cfg(), datetime(2026, 10, 26, 4, 0)) is True
    assert _in_quiet_hours(Cfg(), datetime(2026, 10, 26, 14, 0)) is False


def test_first_json_extracts() -> None:
    from dl_senpai.heybox_browse import _first_json

    assert _first_json('好的\n{"pick": 3, "why": "想看"}\n')["pick"] == 3
    assert _first_json("什么都不想说") == {}
