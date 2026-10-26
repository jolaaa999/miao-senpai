"""角色卡体检单测。

这个体检的价值全在「不误报」上：误报多了就会被当噪音关掉，等于没有。
所以除了「该报的要报」，还专门钉了「反面教材不能报」。
"""

from __future__ import annotations

from dl_senpai.persona import SYSTEM_PROMPT
from dl_senpai.persona_lint import lint_persona, lint_persona_prompt

# 一份最小的合格人设：够长、有说话方式、有边界、没有客服话术
_OK = (
    "【你是谁】\n你是住在群里的学姐，喜欢吐槽，也爱管闲事。\n"
    "【怎么说话】\n短句口语，语气软，打字不打标点，别用 Markdown。\n"
    "【边界】\n不要装客服，不要每句都问对方，不要升华总结。\n"
) * 3


def test_current_persona_passes():
    """线上人设必须零问题——这条红了说明有人改人设改歪了。"""
    assert lint_persona() == []


def test_ok_persona_passes():
    assert lint_persona_prompt(_OK) == []


def test_flags_service_phrase():
    text = _OK + "\n如果有不懂的，希望对你有帮助。"
    problems = lint_persona_prompt(text)
    assert any("客服话术" in p for p in problems)


def test_does_not_flag_negative_examples():
    """反面教材里提到话术是正确用法，不能报警。"""
    text = _OK + "\n【绝对不要说的】\n- 助手腔：「希望对你有帮助」「作为一个 AI」"
    assert lint_persona_prompt(text) == []


def test_does_not_flag_phrase_under_negative_heading():
    """否定词在段标题上（【绝对不要说的】）也要认。"""
    text = _OK + "\n\n【绝对不要说的】\n- 客服腔：「如果你还有需要随时叫我」"
    assert lint_persona_prompt(text) == []


def test_flags_assistant_identity():
    text = _OK + "\n你是一个智能助手。"
    problems = lint_persona_prompt(text)
    assert any("身份里出现" in p for p in problems)


def test_flags_missing_speech_hints():
    # 刻意不出现「说话 / 语气 / 口吻 / 打字」这些词
    text = (
        "【你是谁】\n你是学姐，喜欢吐槽也爱管闲事，群里谁都认识你。\n"
        "【边界】\n不要装客服，不要每句都问对方，不要升华总结，不要编造事实。\n"
    ) * 6
    problems = lint_persona_prompt(text)
    assert any("没写「怎么说话」" in p for p in problems)


def test_flags_missing_boundary():
    # 刻意不出现「不要 / 别 / 禁止 / 不许 / 不准 / 绝不」
    text = (
        "【你是谁】\n你是学姐，喜欢吐槽也爱管闲事，群里谁都认识你。\n"
        "【怎么说话】\n短句，口语化，语气软。\n"
    ) * 6
    problems = lint_persona_prompt(text)
    assert any("没写「不做什么」" in p for p in problems)


def test_flags_markdown_headings():
    text = "# 人设\n" + _OK
    problems = lint_persona_prompt(text)
    assert any("Markdown 标题" in p for p in problems)


def test_flags_overlong_line():
    text = _OK + "\n" + ("很长的一句话" * 120)
    problems = lint_persona_prompt(text)
    assert any("单行超过" in p for p in problems)


def test_flags_empty_persona():
    problems = lint_persona_prompt("")
    assert problems and "被清空" in problems[0]


def test_real_persona_is_not_empty():
    """防止「人设被误删成空串」这种灾难悄无声息地过测试。"""
    assert len(SYSTEM_PROMPT.strip()) > 2000
