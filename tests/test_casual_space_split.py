"""闲聊标点整形（2026-10-27）：逗号换空格、去掉句末句号。

来源：小黑盒《提升大肥鱼活人感技巧》第四条——「逗号一多就书面，空格一多就网上」，
作者说这是他实测收益最大的一条。

为什么必须做成出站整形而不是写进人设：我先写进了人设，干跑照样得到
「累成这样啊，那就先别干了，躺平一会儿。」三个逗号一个没少。
模型对这种细粒度指令基本不服从。
"""

from __future__ import annotations

from dl_senpai.config import PluginConfig
from dl_senpai.tone import is_casual_prompt, space_out_commas, strip_bubble_periods


def test_comma_becomes_space_and_period_dropped():
    out, n = space_out_commas("累成这样啊，那就先别干了，躺平一会儿。")
    assert out == "累成这样啊 那就先别干了 躺平一会儿"
    assert n == 2


def test_question_and_exclamation_survive():
    """问号感叹号是情绪，不是书面感，不能删。"""
    assert space_out_commas("要问啥？")[0] == "要问啥？"
    assert space_out_commas("真的假的！")[0] == "真的假的！"
    assert space_out_commas("哈哈哈……")[0] == "哈哈哈……"


def test_number_and_english_commas_are_protected():
    """数字分隔和英文语法逗号不许动，那是语法不是停顿。"""
    out, n = space_out_commas("why, does this, break")
    assert out == "why, does this, break" and n == 0


def test_code_fence_is_untouched():
    fence = chr(96) * 3
    src = fence + "python" + chr(10) + "x, y = 1, 2" + chr(10) + fence
    out, n = space_out_commas(src)
    assert out == src and n == 0


def test_empty_text_is_safe():
    assert space_out_commas("") == ("", 0)
    assert space_out_commas("   ") == ("   ", 0)


def test_multiline_bubbles_are_each_shaped():
    """拆泡后的每一条都要整形，不是只处理第一行。"""
    out, n = space_out_commas("累成这样啊，先别干了。" + chr(10) + "今天忙啥了，这么耗尽？")
    lines = out.split(chr(10))
    assert lines[0] == "累成这样啊 先别干了"
    assert lines[1] == "今天忙啥了 这么耗尽？"
    assert n == 2


def test_no_double_spaces_or_leading_space():
    out, _ = space_out_commas("行， ，那就这样吧")
    assert "  " not in out
    assert not out.startswith(" ")


def test_switch_is_on_by_default():
    """默认开：不然这轮改动等于没做。"""
    assert PluginConfig().casual_space_split is True



def test_tech_question_is_not_treated_as_casual():
    """干跑查出来的真问题：13 字的技术提问被当闲聊，答案从 292 字压到 28 字。

    用户看到的「一本正经重复废话」，有一部分就是这么来的——
    不是她在说废话，是答案被长度纪律砍掉了。
    """
    for q in [
        "学姐我那个 loss 一直不降",
        "这个报错怎么解决",
        "打不过冰咒龙",
        "帮我配下太刀",
        "pytorch 装不上",
    ]:
        assert is_casual_prompt(q) is False, f"技术问题不能被当闲聊压短：{q}"


def test_real_casual_still_gets_short():
    """别把闲聊也一起放过了——短问题还是要压短。"""
    for q in ["今天好累啊 什么都不想干", "晚上吃啥", "在吗", "学姐你是不是不想理我了"]:
        assert is_casual_prompt(q) is True, f"闲聊应当压短：{q}"


def test_periods_cleared_after_bubble_split():
    """拆泡前只能删整段最后那个句号，中间的句子变成行尾后要再清一次。"""
    src = "都这个点了还没吃？" + chr(10) + "再拖下去就成夜宵了。" + chr(10) + "要不随便来碗面"
    out = strip_bubble_periods(src)
    assert out == "都这个点了还没吃？" + chr(10) + "再拖下去就成夜宵了" + chr(10) + "要不随便来碗面"

