from __future__ import annotations

import random

from dl_senpai.trigger import (
    InterruptCooldown,
    decide_trigger,
    has_dl_keyword,
    should_ignore_text,
)

LONG_MSG = "this is a long enough group message"


def test_ignore_short_and_command() -> None:
    assert should_ignore_text("hi", 8) is True
    assert should_ignore_text("/help", 8) is True
    assert should_ignore_text(LONG_MSG, 8) is False
    assert should_ignore_text("hi", 8, has_images=True) is False
    assert should_ignore_text("", 8, has_images=True) is False


def test_dl_keyword() -> None:
    assert has_dl_keyword("loss exploded again") is True
    assert has_dl_keyword("nice weather today") is False
    assert has_dl_keyword("过拟合怎么办") is True


def test_mention_always() -> None:
    cd = InterruptCooldown()
    d = decide_trigger(
        is_mentioned=True,
        text="hi",
        session_id="g1",
        interrupt_prob=0.0,
        cooldown_sec=999,
        min_msg_len=8,
        keyword_boost=0.0,
        cooldown=cd,
    )
    assert d.kind == "mention"


def test_cooldown_blocks_interrupt() -> None:
    cd = InterruptCooldown()
    cd.mark("g1", now=1000.0)
    d = decide_trigger(
        is_mentioned=False,
        text=LONG_MSG,
        session_id="g1",
        interrupt_prob=1.0,
        cooldown_sec=180,
        min_msg_len=8,
        keyword_boost=0.0,
        cooldown=cd,
        rng=random.Random(0),
        now=1100.0,
    )
    assert d.kind == "none"
    assert d.reason == "cooldown"


def test_interrupt_when_prob_one() -> None:
    cd = InterruptCooldown()
    d = decide_trigger(
        is_mentioned=False,
        text=LONG_MSG,
        session_id="g2",
        interrupt_prob=1.0,
        cooldown_sec=1,
        min_msg_len=8,
        keyword_boost=0.0,
        cooldown=cd,
        rng=random.Random(0),
        now=0.0,
    )
    assert d.kind == "interrupt"


def test_no_interrupt_when_prob_zero() -> None:
    cd = InterruptCooldown()
    d = decide_trigger(
        is_mentioned=False,
        text="training loss exploded what should I do now",
        session_id="g3",
        interrupt_prob=0.0,
        cooldown_sec=1,
        min_msg_len=8,
        keyword_boost=0.0,
        cooldown=cd,
        rng=random.Random(1),
        now=0.0,
    )
    assert d.kind == "none"


def test_attention_window() -> None:
    from dl_senpai.trigger import AttentionWindow

    win = AttentionWindow()
    assert win.active("g1", 42, 60, now=1000.0) is False
    win.arm("g1", 42, now=1000.0)
    assert win.active("g1", 42, 60, now=1030.0) is True
    assert win.active("g1", 42, 60, now=1070.0) is False
    assert win.active("g1", 99, 60, now=1030.0) is False
