"""回复长度闸门的守卫测试。

这里钉的是「不许把话切一半」这件事，不是长度本身——
长度由提示词里的长度纪律管（见 brevity.py），代码这边只负责
别在她说到一半时硬砍。
"""

from __future__ import annotations

from dl_senpai.config import PluginConfig
from dl_senpai.llm import truncate_reply


def test_zero_means_no_truncation():
    """0 = 不截断。这是默认值：日常回复到不了这个量级。"""
    long_text = "很长的一段话。" * 200
    assert truncate_reply(long_text, 0) == long_text.strip()


def test_negative_means_no_truncation():
    # 配错成负数时也当作不限制，而不是把所有回复清空
    assert truncate_reply("正常的一句话。", -1) == "正常的一句话。"


def test_short_text_untouched():
    assert truncate_reply("abc", 10) == "abc"


def test_cuts_at_sentence_boundary_not_mid_word():
    """核心：超长时切在句号处，不出现半句话。"""
    text = "第一句话说完了。第二句话也说完了。第三句话在这里被切断"
    out = truncate_reply(text, 20)
    assert out.endswith("。"), f"应在句号处结束，实际：{out!r}"
    assert "第三句" not in out, "第三句已经被切断，不该出现"
    # 20 字窗口里能装下前两句（8 + 9 = 17 字），所以停在第二句句号
    assert out == "第一句话说完了。第二句话也说完了。"


def test_falls_back_to_hard_cut_without_punctuation():
    """整段没有标点时才硬切（否则会一个字都切不出来）。"""
    assert truncate_reply("abcdefghij", 5) == "abcd…"


def test_punctuation_far_back_still_returns_something():
    """只在很早的地方有句号时，仍然正常返回，不报错。"""
    text = "好。" + "啊" * 100
    out = truncate_reply(text, 30)
    assert out == "好。"


def test_default_config_has_no_hard_cap():
    """默认配置必须是不截断——有人把上限调回来时这条会红。"""
    assert PluginConfig().reply_hard_max_chars == 0
