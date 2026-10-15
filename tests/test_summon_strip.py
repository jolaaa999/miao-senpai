"""召唤词剥离的行为契约。

用户反馈：用「喵喵」唤醒学姐时，她总把这两个字也当成对话内容。
根因是召唤词是普通文字、原样留在正文里（@ 反倒没事——OneBot 的 at 是
独立段，取纯文本时整个被丢弃）。

这里锁死三件事：
1. 剥离本身：删所有出现位置、清残留标点、长短词不互相截断
2. 空值兜底：纯呼叫剥成空时用占位符，而不是发一条空消息
3. 接线：build_agent_context 里必须真的调用它（回归防线）
"""

from __future__ import annotations

import types

import pytest

from dl_senpai.core.agent_trigger import (
    SUMMON_ONLY_PLACEHOLDER,
    has_summon_keyword,
    strip_summon_keywords,
)

KEYWORDS = ["喵喵", "喵学姐"]


# -- 剥离本身 --


def test_strips_leading_keyword() -> None:
    """最基本的情形：唤醒词在句首。"""
    assert strip_summon_keywords("喵喵锐评下我", KEYWORDS) == "锐评下我"


def test_strips_trailing_keyword() -> None:
    """召唤词也可以放句尾。"""
    assert strip_summon_keywords("锐评下我喵喵", KEYWORDS) == "锐评下我"


def test_strips_all_occurrences() -> None:
    """出现多次要全部删掉，不能只删第一个。"""
    assert strip_summon_keywords("喵喵锐评下我喵喵", KEYWORDS) == "锐评下我"


def test_cleans_leading_punctuation() -> None:
    """「喵喵，在吗」删完会剩「，在吗」，句首标点必须清掉。"""
    assert strip_summon_keywords("喵喵，在吗", KEYWORDS) == "在吗"


def test_collapses_duplicate_punctuation() -> None:
    """夹在中间时会留下双标点，要合并成一个。"""
    assert strip_summon_keywords("你好，喵喵，我是小明", KEYWORDS) == "你好，我是小明"


def test_cleans_surrounding_whitespace() -> None:
    assert strip_summon_keywords("  喵喵   在吗  ", KEYWORDS) == "在吗"


def test_longer_keyword_wins() -> None:
    """同时配了「喵喵」和「喵学姐」时，先删短的会留下孤零零的「学姐」。"""
    assert strip_summon_keywords("喵学姐这道题怎么做", KEYWORDS) == "这道题怎么做"


def test_case_insensitive() -> None:
    """召唤词大小写不敏感，剥离也得跟着不敏感（用户可能配英文词）。"""
    assert strip_summon_keywords("Hello SENPAI 你好", ["senpai"]) == "Hello 你好"


def test_regex_metacharacters_in_keyword_are_literal() -> None:
    """召唤词是用户可配的，带正则元字符时不能当成语法。"""
    assert strip_summon_keywords("a.c 剩下", ["a.c"]) == "剩下"


def test_pure_call_becomes_empty() -> None:
    """纯呼叫剥完就是空——占位符由调用方决定，这里只负责删干净。"""
    assert strip_summon_keywords("喵喵喵", KEYWORDS) == ""


def test_repeated_call_is_fully_removed() -> None:
    """连发是打卡任务要求的动作，「喵喵喵」不能剩一个「喵」。

    只删「喵喵」的话，三个字会被吃掉两个、留下一个，正文里多出一个
    无意义的字——这正是把「整数次呼叫 + 半个词」的尾巴也要吃掉的原因。
    """
    assert strip_summon_keywords("喵喵喵喵喵", KEYWORDS) == ""
    assert strip_summon_keywords("喵喵喵在吗", KEYWORDS) == "在吗"


def test_no_keyword_returns_text_unchanged() -> None:
    """没命中时原样返回，好让调用方无脑调用、普通消息不会被改写。"""
    assert strip_summon_keywords("没有召唤词", KEYWORDS) == "没有召唤词"
    # 只有空白也不能被当成「剥完变空」，否则会被误塞占位符
    assert strip_summon_keywords("   ", KEYWORDS) == "   "


def test_empty_inputs_are_safe() -> None:
    assert strip_summon_keywords("", KEYWORDS) == ""
    assert strip_summon_keywords("喵喵在吗", []) == "喵喵在吗"


def test_has_summon_keyword_matches_containment() -> None:
    """判定用「包含」而不是「整条等于」：否则「喵喵在吗」召唤不出来。"""
    assert has_summon_keyword("喵喵在吗", KEYWORDS) is True
    assert has_summon_keyword("在吗", KEYWORDS) is False


# -- 接线：build_agent_context 必须真的调用剥离 --


def _ctx(plain_text: str, **over: object) -> object:
    from dl_senpai.config_runtime.runtime import get_config

    data = dict(
        plain_text=plain_text,
        session_id="group:100",
        group_id="100",
        user_id="7",
        is_private=False,
        extra={"config": get_config()},
        trace_id="t1",
    )
    data.update(over)
    return types.SimpleNamespace(**data)


def test_bridge_strips_summon_word_from_user_text() -> None:
    """回归：桥接层必须真的删掉召唤词。

    只测 strip_summon_keywords 是不够的——那个函数写对了但没人调用的话，
    用户看到的 bug 依旧。这里盯的是接线本身。
    """
    from dl_senpai.legacy_bridge import build_agent_context

    ctx = _ctx("喵喵锐评下我")
    built = build_agent_context(ctx)
    assert "喵喵" not in built.user_text
    assert "锐评下我" in built.user_text


def test_bridge_uses_placeholder_for_pure_call() -> None:
    """纯呼叫不能被剥成空消息——模型收到空输入会答非所问。"""
    from dl_senpai.legacy_bridge import build_agent_context

    ctx = _ctx("喵喵喵")
    built = build_agent_context(ctx)
    assert built.user_text == SUMMON_ONLY_PLACEHOLDER


def test_bridge_leaves_plain_text_untouched() -> None:
    """没有召唤词时原样透传，不能顺手改动正常内容。"""
    from dl_senpai.legacy_bridge import build_agent_context

    ctx = _ctx("今天天气不错")
    built = build_agent_context(ctx)
    assert built.user_text == "今天天气不错"


def test_bridge_keeps_placeholder_out_of_normal_text() -> None:
    """只有空白的正文不该被当成纯呼叫。"""
    from dl_senpai.legacy_bridge import build_agent_context

    ctx = _ctx("   ")
    built = build_agent_context(ctx)
    assert built.user_text != SUMMON_ONLY_PLACEHOLDER
