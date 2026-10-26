"""心情分层：手札 → 底色 → 动态心情（2026-10-25 加）。

思路来自小黑盒「大肥鱼教程篇2：不靠好感度数值的新心脏系统」。
要点：
1. 手札只活三天，同类并排并计数，但**系统不点火**；
2. 底色一天定一次，动态心情白天可以改掉它；
3. 进提示词的是话，不是分数——数字只会让她照着数字演。
"""

from __future__ import annotations

import time

from dl_senpai.inner_state import (
    InnerStateStore,
    build_inner_state_brief,
    notes_summary,
)

DAY = 86400.0


def _store(tmp_path) -> InnerStateStore:
    return InnerStateStore(tmp_path / "inner")


def test_notes_expire_after_three_days(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.add_note("g", "他今天一句话没回", "被冷落", now=now - DAY)
    store.add_note("g", "五天前的旧账", "被冷落", now=now - 5 * DAY)
    summary = notes_summary(store.load("g", now=now), now=now)
    assert "被冷落×1" in summary, "三天外的必须过期"
    assert "×2" not in summary


def test_notes_are_grouped_by_tag_and_do_not_fire(tmp_path):
    """同类并排 + 计数，但不写「她很生气」「该发作了」。"""
    store = _store(tmp_path)
    now = time.time()
    for _ in range(3):
        store.add_note("g", "又没回我", "被冷落", now=now - 3600)
    store.add_note("g", "夸我代码写得好", "被夸", now=now - 7200)
    brief = build_inner_state_brief(store.load("g", now=now), now=now)
    assert "被冷落×3" in brief
    assert "被夸×1" in brief
    assert "生气" not in brief and "发火" not in brief, "系统不点火，由她自己决定"


def test_base_mood_is_overridden_by_dynamic_mood(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.add_note("g", "他这两天话很少", "有点闷", now=now - DAY)
    store.settle("g", now=now)
    brief = build_inner_state_brief(store.load("g", now=now), now=now)
    assert "有点闷" in brief

    store.set_mood("g", "没那么闷了", now=now + 60)
    brief2 = build_inner_state_brief(store.load("g", now=now + 120), now=now + 120)
    assert "没那么闷了" in brief2
    assert "本来有点闷" in brief2, "底色要留着，读起来才像「今天缓过来了」"


def test_brief_never_contains_numbers(tmp_path):
    """心情不再以分数形式进提示词。"""
    store = _store(tmp_path)
    now = time.time()
    store.add_note("g", "有点累", "倦", now=now)
    store.settle("g", now=now)
    brief = build_inner_state_brief(store.load("g", now=now), now=now)
    assert "mood=" not in brief and "energy=" not in brief


def test_empty_state_stays_silent(tmp_path):
    store = _store(tmp_path)
    assert build_inner_state_brief(store.load("g"), now=time.time()) == ""
