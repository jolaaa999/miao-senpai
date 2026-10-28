"""接话原则 + 例外样例（2026-10-27/28）。

为什么是两段：字面样例会被连句子形状一起抄，样例没覆盖的场合抄不到就退回助手腔，
所以主力是原则；但「被占便宜时怎么顶回去」这种机智写不出原则，留 10 对当手感。
两段的场合必须分开，混在一起就又变回台词本。
"""

from __future__ import annotations

from dl_senpai.persona import (
    EXCEPTION_SAMPLES,
    FEWSHOT_DIALOGUES,
    REPLY_PRINCIPLES,
    build_system_prompt,
    format_fewshot_block,
)


def test_main_dialogues_stay_empty() -> None:
    """主力样例必须还是空的——台词一旦进主力段就会盖过原则。"""
    assert FEWSHOT_DIALOGUES == ()


def test_exception_samples_are_few_and_short() -> None:
    """例外样例只能是个位数、而且必须短。

    它们存在的意义是「手感」，多了就变成模板；长了会把她默认回复拉长。
    """
    assert len(EXCEPTION_SAMPLES) <= 10
    for user, assistant in EXCEPTION_SAMPLES:
        assert len(assistant) <= 12, f"例外样例太长了：{assistant}"


def test_block_is_principles_then_exceptions() -> None:
    block = format_fewshot_block()
    assert "【这类消息怎么接】" in block
    assert "台词本" in block
    assert "【例外样例｜只在被占便宜、被乱起外号、被抬杠的时候参考】" in block
    # 原则必须在例外之前：先立判据，再给手感
    assert block.index("【这类消息怎么接】") < block.index("【例外样例")
    assert len(REPLY_PRINCIPLES) >= 8


def test_exception_lines_only_inside_that_section() -> None:
    """台词只许出现在例外段里：出现在前面就等于又变回台词本。"""
    block = format_fewshot_block()
    cut = block.index("【例外样例")
    head, tail = block[:cut], block[cut:]
    for line in ("磕你个头", "你才是猫娘", "？就这？"):
        assert line not in head, f"台词 {line} 漏进原则段了"
        assert line in tail


def test_principles_cover_key_scenes() -> None:
    """场景覆盖：少哪一类，那一类就退回通用助手腔。"""
    blob = chr(10).join(REPLY_PRINCIPLES)
    for scene in ("打招呼", "被夸", "身份", "抱怨", "真倾诉", "技术求助", "求建议"):
        assert scene in blob, f"原则里没有 {scene} 这一类"


def test_build_system_prompt_injects_principle_block_when_enabled() -> None:
    with_fs = build_system_prompt(fewshot_enable=True, interrupt=False)
    assert "【这类消息怎么接】" in with_fs
    without = build_system_prompt(fewshot_enable=False, interrupt=False)
    assert "【这类消息怎么接】" not in without


def test_block_skipped_on_interrupt() -> None:
    prompt = build_system_prompt(fewshot_enable=True, interrupt=True)
    assert "【这类消息怎么接】" not in prompt

