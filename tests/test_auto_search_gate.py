# -*- coding: utf-8 -*-
"""自动搜索门控：闲聊不许搜，该搜的要搜。

背景（2026-10-24 实测）：should_auto_search 里有一条
「_QUERY_TERM_RE(任意两个汉字) + 长度>=8」，导致中文闲聊几乎必触发检索，
且首轮无结果时还会再套 site:bilibili/攻略/2026 三个兜底词，共 5 轮全是无关内容。
"""

from __future__ import annotations

import pytest

from dl_senpai.web_search import should_auto_search


@pytest.mark.parametrize(
    'text',
    [
        '补药再踢了，可以免打扰',
        '踢了吧，等我调好我再拉进来玩',
        '你也可以找 签到',
        '那我们晚上八点上号打劫吧',
        '刚吃完饭，歇一会儿',
        '这波我认了，下次再来',
    ],
)
def test_chitchat_does_not_trigger_search(text: str) -> None:
    """日常对话一个字都不该去联网。"""
    assert should_auto_search(text) is False


@pytest.mark.parametrize(
    'text',
    [
        '永劫无间新赛季什么时候更新',
        '星穹铁道最新补丁改了什么',
        '帮我搜一下武科大图书馆电话',
        '今天武汉天气怎么样',
    ],
)
def test_factual_questions_still_trigger_search(text: str) -> None:
    """该搜的照搜：时效、热点、明确要求搜索。"""
    assert should_auto_search(text) is True


def test_aggressive_flag_no_longer_rescues_chitchat() -> None:
    """即使显式打开 aggressive，闲聊也不该被判定成事实查询。"""
    assert should_auto_search('补药再踢了，可以免打扰', aggressive=True) is False



@pytest.mark.parametrize(
    'text',
    [
        '过拟合怎么办',
        '梯度消失怎么解决',
        '链表反转怎么写',
    ],
)
def test_textbook_concepts_do_not_trigger_search(text: str) -> None:
    """课本/基础概念自己就会，不必联网（与 grounding 的清单保持一致）。"""
    assert should_auto_search(text) is False


@pytest.mark.parametrize(
    'text',
    [
        '冰弓前期配装怎么搭',
        '这套轻弩怎么配技能',
    ],
)
def test_how_questions_about_game_still_search(text: str) -> None:
    """带「怎么」的真实打听要搜，不能被防闲聊的规则误杀。"""
    assert should_auto_search(text) is True


def test_emotional_how_is_not_a_query() -> None:
    """「怎么这么好吃」是感叹，不是打听。"""
    assert should_auto_search('这奶茶怎么这么好喝') is False
