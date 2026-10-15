"""Phase 6A：Legacy marker 行为快照（迁移前 vs 迁移后）。

第二十三节要求：对 8 个 marker 建立
    LEGACY before  vs  LEGACY after shared adapter
的行为对照，至少比较 ToolCall name / arguments / order /
malformed behavior / tolerance behavior。

本文件的"before"基线来自 Phase 5.5 之前 `llm._finalize_result` 的行为，
已记录在 `tests/marker_baseline.py`（容错矩阵）与此处的显式断言中。

迁移后 `_finalize_result` 委托给共享 `MarkerToolAdapter`，
因此这些断言证明：**换了解析实现，行为没变**。
"""

from __future__ import annotations

from typing import Any

import pytest

from dl_senpai.config import PluginConfig
from dl_senpai.llm import SenpaiLLM
from dl_senpai.observability.retry import NoWaitRetryPolicy
from tests.marker_baseline import BARE_MARKER, TOLERANCE


def make_llm() -> SenpaiLLM:
    cfg = PluginConfig(
        OPENAI_API_KEY="sk-test",
        OPENAI_BASE_URL="https://example.com/v1",
        OPENAI_MODEL="test-model",
        DL_SENPAI_REPLY_HARD_MAX_CHARS=8000,
    )
    return SenpaiLLM(cfg, retry=NoWaitRetryPolicy())


def finalize(
    text: str,
    *,
    card: bool = False,
    mute: bool = False,
    draw: bool = False,
    browse: bool = False,
    person_memory: bool = False,
    searched: bool = False,
) -> Any:
    return make_llm()._finalize_result(
        text,
        card_enable=card,
        mute_enable=mute,
        searched=searched,
        draw_enable=draw,
        browser_enable=browse,
        person_memory_enable=person_memory,
    )


# ================================================================
# 1. 单 marker：name / arguments
# ================================================================


def test_card_arguments_preserved() -> None:
    r = finalize(
        '好\n<<<CARD {"user_id":123,"card":"小鱼干","kind":"request"}>>>',
        card=True,
    )
    assert len(r.card_actions) == 1
    action = r.card_actions[0]
    assert action.user_id == 123
    assert action.card == "小鱼干"
    assert action.kind == "request"
    assert "CARD" not in r.text


def test_mute_arguments_preserved() -> None:
    r = finalize(
        '好\n<<<MUTE {"user_id":456,"duration":60,"kind":"request"}>>>',
        mute=True,
    )
    assert len(r.mute_actions) == 1
    assert r.mute_actions[0].user_id == 456
    assert r.mute_actions[0].duration == 60


def test_search_marker_stripped_and_sets_searched() -> None:
    """SEARCH marker 被剥离，且解析层会把 `searched` 置为 True。

    与重构前的细微差异（语义等价但更直接）：
    重构前 `_finalize_result` 透传调用方传入的 `searched`，而调用方
    （`llm.chat` 的循环）在调用它**之前**就已经累加过检索标记。
    现在解析层只要看到 SEARCH marker 就置位，结果一致。

    调用方传入的 `searched=True`（多轮检索累加）仍会被保留。
    """
    r = finalize("<<<SEARCH 今日天气>>>", searched=False)
    assert "SEARCH" not in r.text
    assert r.searched is True, "解析到 SEARCH marker 应置位 searched"

    # 多轮检索的累加值不能被解析层抹掉
    r2 = finalize("普通文本", searched=True)
    assert r2.searched is True


def test_draw_prompt_preserved() -> None:
    r = finalize("图\n<<<DRAW a cute cat>>>", draw=True)
    assert r.draw_prompt == "a cute cat"
    assert r.draw_force is True


def test_voice_override_preserved() -> None:
    r = finalize("在\n<<<VOICE 晚安啦>>>")
    assert r.voice_force is True
    assert r.voice_override == "晚安啦"


def test_sticker_query_preserved() -> None:
    r = finalize("哼\n<<<STICKER 傲娇>>>")
    assert r.sticker_force is True
    assert r.sticker_query == "傲娇"


def test_impression_preserved() -> None:
    r = finalize(
        '<<<IMPRESSION {"add":["爱猫"],"remove":[],"notes":null}>>>',
        person_memory=True,
    )
    assert len(r.impression_updates) == 1
    assert "爱猫" in r.impression_updates[0].add

def test_browse_tasks_preserved() -> None:
    r = finalize("找\n<<<BROWSE pixiv 猫>>>", browse=True)
    assert r.browse_force is True
    assert len(r.browse_tasks) == 1


# ================================================================
# 2. 多 marker：order 稳定性
# ================================================================


def test_multi_marker_all_parsed() -> None:
    """同一回复多个 marker 时全部解析成功。"""
    r = finalize(
        "好呀\n"
        '<<<CARD {"user_id":1,"card":"x","kind":"request"}>>>\n'
        "<<<VOICE 晚安>>>\n"
        "<<<STICKER 得意>>>",
        card=True,
    )
    assert len(r.card_actions) == 1
    assert r.voice_force is True
    assert r.sticker_force is True
    for marker in ("CARD", "VOICE", "STICKER", "<<"):
        assert marker not in r.text


def test_multi_marker_order_stable_across_calls() -> None:
    """多次调用结果一致（解析顺序稳定）。"""
    text = (
        '<<<CARD {"user_id":1,"card":"x","kind":"request"}>>>\n'
        "<<<VOICE 晚安>>>"
    )
    first = finalize(text, card=True)
    second = finalize(text, card=True)
    assert len(first.card_actions) == len(second.card_actions) == 1
    assert first.voice_override == second.voice_override == "晚安"


def test_two_card_markers_both_captured() -> None:
    r = finalize(
        '<<<CARD {"user_id":1,"card":"a","kind":"request"}>>>\n'
        '<<<CARD {"user_id":2,"card":"b","kind":"request"}>>>',
        card=True,
    )
    assert len(r.card_actions) == 2
    assert {a.user_id for a in r.card_actions} == {1, 2}


# ================================================================
# 3. 容错闭合（必须与 marker_baseline 一致）
# ================================================================


@pytest.mark.parametrize("closer,parses", list(TOLERANCE["SEARCH"].items()))
def test_search_tolerance_unchanged(closer: str, parses: bool) -> None:
    r = finalize(f"<<<SEARCH 天气{closer}")
    if parses:
        assert "SEARCH" not in r.text, f"closer={closer} 应被解析并剥离"
    else:
        assert "SEARCH" in r.text, f"closer={closer} 不应被解析"


@pytest.mark.parametrize("closer,parses", list(TOLERANCE["DRAW"].items()))
def test_draw_tolerance_unchanged(closer: str, parses: bool) -> None:
    r = finalize(f"图\n<<<DRAW a cat{closer}", draw=True)
    if parses:
        assert r.draw_prompt == "a cat"
    else:
        assert r.draw_prompt != "a cat"


@pytest.mark.parametrize("closer,parses", list(TOLERANCE["CARD"].items()))
def test_card_tolerance_strict(closer: str, parses: bool) -> None:
    """CARD 只认 `>>>` —— 历史行为，迁移后必须保持。"""
    r = finalize(
        f'好\n<<<CARD {{"user_id":1,"card":"x","kind":"request"}}{closer}',
        card=True,
    )
    if parses:
        assert len(r.card_actions) == 1
    else:
        assert len(r.card_actions) == 0


@pytest.mark.parametrize("closer,parses", list(TOLERANCE["MUTE"].items()))
def test_mute_tolerance_strict(closer: str, parses: bool) -> None:
    r = finalize(
        f'好\n<<<MUTE {{"user_id":1,"duration":60,"kind":"request"}}{closer}',
        mute=True,
    )
    if parses:
        assert len(r.mute_actions) == 1
    else:
        assert len(r.mute_actions) == 0


@pytest.mark.parametrize("closer,parses", list(TOLERANCE["VOICE"].items()))
def test_voice_tolerance_unchanged(closer: str, parses: bool) -> None:
    r = finalize(f"在\n<<<VOICE 晚安{closer}")
    if parses:
        assert r.voice_force is True
    else:
        assert r.voice_force is False


@pytest.mark.parametrize("closer,parses", list(TOLERANCE["IMPRESSION"].items()))
def test_impression_tolerance_strict(closer: str, parses: bool) -> None:
    r = finalize(
        f'<<<IMPRESSION {{"add":["爱猫"],"remove":[],"notes":null}}{closer}',
        person_memory=True,
    )
    if parses:
        assert len(r.impression_updates) == 1
    else:
        assert len(r.impression_updates) == 0


# ================================================================
# 4. Bare marker 行为
# ================================================================


def test_bare_draw_marker() -> None:
    r = finalize("图\n<<<DRAW>>>", draw=True)
    assert r.draw_force is True


def test_bare_voice_marker() -> None:
    r = finalize("在\n<<<VOICE>>>")
    assert r.voice_force is True
    assert r.voice_override == ""


def test_bare_sticker_not_parsed() -> None:
    """裸 <<<STICKER>>> 不解析（与基线一致）。"""
    r = finalize("哼\n<<<STICKER>>>")
    assert r.sticker_force is False


def test_bare_browse_marker() -> None:
    r = finalize("找\n<<<BROWSE>>>", browse=True)
    assert r.browse_force is True


# ================================================================
# 5. Malformed 行为
# ================================================================


def test_card_bad_json_no_action_but_stripped() -> None:
    r = finalize("好\n<<<CARD {bad json}>>>", card=True)
    assert r.card_actions == []
    assert "CARD" not in r.text


def test_mute_bad_json_no_action_but_stripped() -> None:
    r = finalize("好\n<<<MUTE {bad}>>>", mute=True)
    assert r.mute_actions == []
    assert "MUTE" not in r.text


def test_impression_bad_json_no_update() -> None:
    r = finalize("<<<IMPRESSION {bad}>>>", person_memory=True)
    assert r.impression_updates == []


def test_card_missing_field_ignored() -> None:
    """缺 card 字段 → 解析层丢弃（与基线一致）。"""
    r = finalize('<<<CARD {"user_id":1}>>>', card=True)
    assert r.card_actions == []


def test_unclosed_marker_is_stripped_by_tail_rule() -> None:
    """未闭合的 `<<<SEARCH` 会被**尾部规则**剥掉（连同后续文本）。

    这是 web_search.py 的 `_SEARCH_MARKER_TAIL_RE` 既有行为：
    模型常在回答末尾写半截 marker，留着会泄漏给群友。
    迁移后行为保持不变。
    """
    r = finalize("<<<SEARCH 天气", searched=False)
    assert "SEARCH" not in r.text, "未闭合 SEARCH 也应被剥离（尾部规则）"
    assert "<<" not in r.text


# ================================================================
# 6. 未启用工具的 marker 必须被剥离（防泄漏）
# ================================================================


def test_disabled_card_marker_stripped() -> None:
    r = finalize(
        '好\n<<<CARD {"user_id":1,"card":"x","kind":"request"}>>>', card=False
    )
    assert r.card_actions == []
    assert "CARD" not in r.text, "未启用时 marker 仍须剥离，防止泄漏给群友"


def test_disabled_mute_marker_stripped() -> None:
    r = finalize(
        '好\n<<<MUTE {"user_id":1,"duration":60,"kind":"request"}>>>', mute=False
    )
    assert r.mute_actions == []
    assert "MUTE" not in r.text


def test_disabled_draw_marker_stripped() -> None:
    r = finalize("好\n<<<DRAW a cat>>>", draw=False)
    assert r.draw_force is False
    assert "DRAW" not in r.text


def test_disabled_browse_marker_stripped() -> None:
    r = finalize("好\n<<<BROWSE pixiv 猫>>>", browse=False)
    assert r.browse_tasks == []
    assert "BROWSE" not in r.text


def test_disabled_impression_marker_stripped() -> None:
    r = finalize(
        '好\n<<<IMPRESSION {"add":["爱猫"],"remove":[],"notes":null}>>>',
        person_memory=False,
    )
    assert r.impression_updates == []
    assert "IMPRESSION" not in r.text


# ================================================================
# 7. 文本清理不变
# ================================================================


def test_plain_text_preserved() -> None:
    r = finalize("就是普通聊天")
    assert r.text == "就是普通聊天"


def test_no_marker_leak_in_any_case() -> None:
    """任何情况下正文都不应出现 `<<<`。"""
    cases = [
        ("<<<SEARCH x>>>", {}),
        ('<<<CARD {"user_id":1,"card":"x","kind":"request"}>>>', {"card": True}),
        ('<<<MUTE {"user_id":1,"duration":60,"kind":"request"}>>>', {"mute": True}),
        ("<<<DRAW x>>>", {"draw": True}),
        ("<<<BROWSE pixiv x>>>", {"browse": True}),
        ("<<<VOICE x>>>", {}),
        ("<<<STICKER x>>>", {}),
        ('<<<IMPRESSION {"add":["爱猫"],"remove":[],"notes":null}>>>', {"person_memory": True}),
    ]
    for text, kwargs in cases:
        r = finalize(text, **kwargs)
        assert "<<" not in r.text, f"{text!r} 泄漏了 marker: {r.text!r}"
