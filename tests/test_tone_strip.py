"""安慰腔小句删除（确定性出站过滤）单测。

为什么要有这一层：模型对「别安慰」的服从度实测压不下来，
让它判、让它改、带人格重说三条路都输出了不能发的东西。
既然改不动它的嘴，就在出站前把安慰那个小句剪掉。

粒度是逗号级：整句砍会把有用的话一起扔了。
兜底是「删空就返回原文」：宁可留一条安慰，也不能发一条空的。
"""

from __future__ import annotations

from dl_senpai.tone import strip_comfort_clauses


def test_drops_comfort_clauses_keeps_the_rest():
    text = "抱抱，两点也太狠了😭 今天先补觉、别硬撑，身体优先。"
    out = strip_comfort_clauses(text)
    assert "抱抱" not in out
    assert "别硬撑" not in out
    assert "身体优先" not in out
    # 有用的话必须留下
    assert "两点也太狠了" in out
    assert "今天先补觉" in out


def test_drops_whole_comfort_sentence():
    out = strip_comfort_clauses("辛苦了，写到两点太拼了。作业拆小块做吧。")
    assert "辛苦" not in out
    assert "作业拆小块做吧" in out


def test_falls_back_to_original_when_everything_is_comfort():
    """整条都是安慰时保留原文：不能发空消息。"""
    text = "先睡吧，别熬了，身体扛不住。"
    assert strip_comfort_clauses(text) == text


def test_leaves_clean_reply_untouched():
    for text in ("两点睡，活该困。", "课设重做就重做，能咋办。"):
        assert strip_comfort_clauses(text) == text


def test_empty_input_safe():
    assert strip_comfort_clauses("") == ""
    assert strip_comfort_clauses("   ") == ""
