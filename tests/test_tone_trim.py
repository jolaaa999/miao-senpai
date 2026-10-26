"""随口一句时的整句截断（确定性出站过滤）单测。

为什么要有：字数是这个模型唯一压不住的东西。人设里写、提示词末尾写、
本轮直接写「别超过 30 字」都没用，该 164 字还是 164 字。

与硬截断的区别（用户明确否过「说到一半停住」）：
只切在**句末标点**之后，永远整句保留；只有一句时宁可超预算也不吐半句。
"""

from __future__ import annotations

from dl_senpai.tone import trim_to_sentence_budget


def test_trims_at_sentence_boundary():
    text = "课设重做确实烦。反正也是抄的，改改就完事。别想着从头写了，来不及。"
    out = trim_to_sentence_budget(text, 20)
    assert out == "课设重做确实烦。"
    assert out.endswith("。"), "必须切在句末标点后"


def test_keeps_multiple_sentences_under_budget():
    text = "太真实了。早八就是反人类。"
    out = trim_to_sentence_budget(text, 60)
    assert out == text, "预算够就不该动"


def test_never_cuts_a_single_clause():
    """整段就是一个小句时，宁可超预算也不切——切了就是半句话。"""
    text = "反正就是那种你懂的说不清但是确实很麻烦的情况"
    out = trim_to_sentence_budget(text, 10)
    assert out == text


def test_falls_back_to_comma_when_no_sentence_end():
    """没有句号时退到逗号级。

    为什么必须有这条兜底：清单被 collapse_multiline 压平之后是一个长句
    「懒人：番茄鸡蛋面，热闹：火锅，清淡：粥+小菜」，一句句号都没有。
    只按句号切的话整段原样发出，实测问「晚上吃啥」回了 89 字菜单。
    切在逗号后仍是完整小句，不算说半句话。
    """
    text = "懒人吃番茄鸡蛋面，热闹就火锅烧烤，清淡是粥加小菜，外卖也行"
    out = trim_to_sentence_budget(text, 20)
    assert len(out) <= 20
    assert out.startswith("懒人吃番茄鸡蛋面")
    # 不能留个孤零零的逗号在结尾
    assert not out.endswith(("，", ",", "、", "；", ";"))


def test_zero_budget_disables():
    text = "第一句。第二句。第三句。"
    assert trim_to_sentence_budget(text, 0) == text


def test_empty_safe():
    assert trim_to_sentence_budget("", 60) == ""
    assert trim_to_sentence_budget("   ", 60).strip() == ""


def test_newline_counts_as_boundary():
    text = "第一行内容\n第二行内容\n第三行内容"
    out = trim_to_sentence_budget(text, 12)
    assert "第三行" not in out
