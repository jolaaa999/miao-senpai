"""出站过滤不能吃掉工具标记（名片/表情包/语音）。

这条是线上事故换来的：截断逻辑第一版把 <<<CARD ...>>> 一起切掉了，
日志里 before_len=64 → after_len=7，回复看着正常、名片却没改。
外面完全看不出来，只有快照测试能抓到。
"""

from __future__ import annotations

from dl_senpai.tone import strip_comfort_clauses, trim_to_sentence_budget

CARD = '<<<CARD {"user_id":123456789,"card":"小明","kind":"request"}>>>'
STICKER = "<<<STICKER 委屈>>>"


def test_trim_keeps_marker_lines():
    text = f"抱抱，别硬撑。第二句本来会被截掉。\n{STICKER}"
    out = trim_to_sentence_budget(text, 10)
    assert STICKER in out, "标记行被截断了"
    assert "第二句" not in out


def test_strip_keeps_marker_lines():
    text = f"辛苦了，早点睡吧。\n{CARD}"
    out = strip_comfort_clauses(text)
    assert CARD in out, "标记行被当成安慰删掉了"


def test_marker_only_reply_untouched():
    """整条只有标记时，两个过滤器都不能动它。"""
    assert trim_to_sentence_budget(CARD, 10) == CARD
    assert strip_comfort_clauses(CARD) == CARD
