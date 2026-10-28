"""出站质检单测：气泡断点与中文空格。

这两条都是「宁可漏修，不能硬粘」的规则，所以测试里除了正例，
每条反例也要钉住——反例退化才会真的把两句独立的话粘成一句。
"""

from __future__ import annotations

from dl_senpai.bubble_lint import (
    fix_cjk_spaces,
    merge_incomplete_bubbles,
    should_join,
)


def test_cjk_space_becomes_comma() -> None:
    assert fix_cjk_spaces("好的 我看下") == "好的，我看下"
    assert fix_cjk_spaces("那个 你有没有 那个") == "那个，你有没有，那个"


def test_latin_and_number_spaces_are_kept() -> None:
    # 英文单词、数字与单位之间的空格是必要的，动了就是新错误
    assert fix_cjk_spaces("用 DeepSeek V3 跑") == "用 DeepSeek V3 跑"
    assert fix_cjk_spaces("加 3 kg 水") == "加 3 kg 水"


def test_no_space_returns_same_string() -> None:
    assert fix_cjk_spaces("就这样了") == "就这样了"
    assert fix_cjk_spaces("") == ""


def test_half_sentence_bubbles_are_merged() -> None:
    assert merge_incomplete_bubbles(["这是哪个番的", "表情包？"]) == [
        "这是哪个番的表情包？"
    ]
    assert merge_incomplete_bubbles(["前两句是宣传，", "第三句暴露了经典"]) == [
        "前两句是宣传，第三句暴露了经典"
    ]


def test_complete_words_are_not_merged() -> None:
    # 「好的」「算了」是完整一句，粘上去就变成「好的我马上来」以外的东西
    assert merge_incomplete_bubbles(["好的", "我马上来"]) == ["好的", "我马上来"]
    assert merge_incomplete_bubbles(["算了", "不问了"]) == ["算了", "不问了"]


def test_particle_tails_are_not_merged() -> None:
    # 「了/吗/呢/吧」结尾通常是说完，典型反例就是这一对
    assert merge_incomplete_bubbles(["你怎么了", "没事吧"]) == ["你怎么了", "没事吧"]


def test_latin_continuation_is_not_merged() -> None:
    # 下一条不是中文接着往下说，就不是「一句话被拆开」
    assert merge_incomplete_bubbles(["我觉得", "ok"]) == ["我觉得", "ok"]


def test_single_bubble_untouched() -> None:
    assert merge_incomplete_bubbles(["就这样"]) == ["就这样"]
    assert merge_incomplete_bubbles([]) == []


def test_merge_respects_length_cap() -> None:
    prev = "这个"
    nxt = "字" * 200
    assert should_join(prev, nxt, max_chars=160) is False
    assert should_join(prev, nxt, max_chars=0) is True
