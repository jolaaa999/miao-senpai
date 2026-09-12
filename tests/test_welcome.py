from __future__ import annotations

import random

from dl_senpai.welcome import build_welcome_text


def test_welcome_contains_name() -> None:
    text = build_welcome_text("小明", rng=random.Random(0))
    assert "小明" in text
    assert "百度迎新" in text or "学姐" in text


def test_welcome_fallback_name() -> None:
    text = build_welcome_text("  ", rng=random.Random(1))
    assert "新同学" in text
    assert "学姐" in text
