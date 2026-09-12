from __future__ import annotations

import shutil
from pathlib import Path

from dl_senpai.inner_state import (
    InnerState,
    InnerStateStore,
    build_inner_state_brief,
    interrupt_prob_multiplier,
    scope_key,
)

_TEST_ROOT = Path(__file__).resolve().parent / "_tmp_inner_state"


def _fresh_dir(name: str) -> Path:
    path = _TEST_ROOT / name
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True, exist_ok=True)
    return path


def test_scope_key() -> None:
    assert scope_key(per_group=True, group_id=123, private=False) == "group_123"
    assert scope_key(per_group=False, group_id=123, private=False) == "global"
    assert scope_key(per_group=True, group_id=None, private=True) == "private"


def test_interrupt_multiplier_low_social() -> None:
    low = InnerState(mood=50, energy=20, social=20)
    high = InnerState(mood=50, energy=90, social=90)
    assert interrupt_prob_multiplier(low) < interrupt_prob_multiplier(high)
    assert interrupt_prob_multiplier(low) >= 0.35


def test_brief_empty_when_neutral() -> None:
    st = InnerState(mood=55, energy=55, social=55)
    assert build_inner_state_brief(st) == ""


def test_brief_when_tired() -> None:
    st = InnerState(mood=50, energy=20, social=50)
    brief = build_inner_state_brief(st)
    assert "【当前状态】" in brief
    assert "懒" in brief or "累" in brief


def test_store_persist_and_apply() -> None:
    store = InnerStateStore(_fresh_dir("persist"))
    st = store.load("group_1")
    assert 0 <= st.energy <= 100
    after = store.apply_after_reply(
        "group_1",
        reply_len=200,
        mentioned=True,
        interrupt=False,
    )
    assert after.energy < 90
    loaded = store.load("group_1")
    assert abs(loaded.energy - after.energy) < 5
