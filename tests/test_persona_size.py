"""人设体积守门 + 关键指令守门（2026-10-27）。

来源：小黑盒《提升大肥鱼活人感技巧》十三条。两条直接落地成测试：

1. **人格卡会膨胀，膨胀到最后她记不住自己是谁**。作者的实测是：
   从 3000 字涨到 13000 字还撑得住，13000 再往 20000 涨就开始出问题。
   我们现在的常驻人格（SYSTEM_PROMPT + 消息格式 + 说话样例）约 4700 字，
   离那条线还有空间——但「每加一条规则就删一条旧规则」这件事没人守，
   很快就会重演。所以设死上限：加规则必须同时删规则。

2. **「允许说废话、允许不热情」是最容易被后来的人删掉的那类规则**。
   它看起来像废话，删掉也不报错，但删掉之后回复会慢慢变成「每句都关心、
   每句都反问」。这类规则必须有测试钉住，否则下次重构就没了。
"""

from __future__ import annotations

from dl_senpai import persona

# 上限不是随便定的：帖子给的实测拐点是 13000 字，这里留一半余量，
# 因为运行时还会叠加工具说明、记忆、群风格等段落。
_SYSTEM_PROMPT_LIMIT = 3700
_FEWSHOT_LIMIT = 1500
_RESIDENT_TOTAL_LIMIT = 5300


def test_system_prompt_does_not_balloon():
    size = len(persona.SYSTEM_PROMPT)
    assert size <= _SYSTEM_PROMPT_LIMIT, (
        f"人设主段 {size} 字，超过 {_SYSTEM_PROMPT_LIMIT}。"
        "加规则之前先删一条旧规则——帖子里那句「每加一条规则就删一条」就是为了这个。"
    )


def test_fewshot_block_does_not_balloon():
    size = len(persona.format_fewshot_block())
    assert size <= _FEWSHOT_LIMIT, f"说话样例 {size} 字，超过 {_FEWSHOT_LIMIT}"


def test_resident_persona_total_stays_small():
    total = (
        len(persona.SYSTEM_PROMPT)
        + len(persona.format_fewshot_block())
        + len(persona.MESSAGE_FORMAT_SYSTEM_EXTRA)
    )
    assert total <= _RESIDENT_TOTAL_LIMIT, (
        f"常驻人格合计 {total} 字，超过 {_RESIDENT_TOTAL_LIMIT}。"
        "常驻部分每一轮都要吃 token，也会稀释她自己的注意力。"
    )


def test_allows_meaningless_talk_and_cold_days():
    """允许说废话、允许不热情——最像人，也最容易被删。"""
    sp = persona.SYSTEM_PROMPT
    assert "允许说废话" in sp
    assert "不想说话" in sp
    assert "客服" in sp, "「这四条凑齐了就是客服」那条正向约束不能丢"


def test_space_instead_of_comma_for_casual_pauses():
    """帖子第④条：句中不用逗号、用空格断句，作者说这条收益最大。"""
    assert "想停顿就用空格" in persona.SYSTEM_PROMPT


def test_fewshot_is_labelled_as_direction_not_script():
    """样例必须是「方向」，不能变成台词本——否则她会照着复读。"""
    block = persona.format_fewshot_block()
    assert "方向" in block
    assert "台词本" in block
    assert "按方向走" in block, "「只看方向、别背句子」这句不能丢"

