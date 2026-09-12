from __future__ import annotations

from dl_senpai.grounding import (
    assess_search_detail_level,
    build_grounding_retry_user_message,
    find_ungrounded_terms,
    requires_strict_grounding,
    should_grounding_retry,
)


def test_requires_strict_grounding_game_build() -> None:
    assert requires_strict_grounding("冰属性弓前期配装怎么搭")
    assert requires_strict_grounding("三角洲乌鲁鲁技能有哪些")
    assert not requires_strict_grounding("过拟合怎么办")
    assert not requires_strict_grounding("晚安学姐")


def test_assess_search_detail_level() -> None:
    assert assess_search_detail_level("") == "none"
    assert assess_search_detail_level("【联网检索结果】\n（没有搜到可用结果）") == "none"
    shallow = "【联网检索结果】\n1. 三角洲行动官网\n   官方网站首页"
    assert assess_search_detail_level(shallow) == "shallow"
    detailed = "【联网检索结果】\n1. 乌鲁鲁技能介绍\n   技能效果：……"
    assert assess_search_detail_level(detailed) == "detailed"


def test_find_ungrounded_terms_catches_fabricated_gear() -> None:
    reply = "前期可以先用耳塞羽饰和冰弓，别为了耳塞牺牲强弓珠。"
    context = "怪物猎人世界 冰弓 配装 强弓珠 耳塞技能"
    bad = find_ungrounded_terms(reply, context)
    assert "耳塞羽饰" in bad


def test_should_grounding_retry_on_shallow_search() -> None:
    reply = "乌鲁鲁技能是烟雾弹、干扰和拉扯。"
    brief = "【联网检索结果】\n1. 三角洲行动官网\n   首页"
    retry, terms = should_grounding_retry(
        reply,
        allowed_context=f"{brief}\n乌鲁鲁技能",
        strict=True,
        detail_level="shallow",
    )
    assert retry is True
    assert terms


def test_build_grounding_retry_message() -> None:
    msg = build_grounding_retry_user_message(["耳塞羽饰", "假头盔"])
    assert "耳塞羽饰" in msg
    assert "瞎编" in msg or "没依据" in msg
