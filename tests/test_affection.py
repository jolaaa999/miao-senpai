from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from dl_senpai.affection import (
    AffectionGainResult,
    AffectionStore,
    AFFECTION_TIER_TITLES,
    affection_scope_private,
    build_affection_brief,
    format_gain_footnote,
    format_help,
    is_private_affection_scope,
    next_tier_for_value,
    parse_affection_command,
    pick_tier_title,
    tier_for_value,
)

_WORK_TMP = Path(__file__).resolve().parents[1] / ".pytest_tmp"


@pytest.fixture
def store() -> AffectionStore:
    _WORK_TMP.mkdir(parents=True, exist_ok=True)
    root = _WORK_TMP / f"affection-{uuid.uuid4().hex}"
    root.mkdir(parents=True, exist_ok=True)
    return AffectionStore(root)


def test_tier_progression() -> None:
    assert tier_for_value(0)[1] == "路人"
    assert tier_for_value(39)[1] == "面熟"
    assert tier_for_value(40)[1] == "常来"
    assert tier_for_value(350)[1] == "心头好"
    assert tier_for_value(500)[1] == "热恋"
    assert tier_for_value(750)[1] == "眷侣"
    assert next_tier_for_value(10) == (15, "面熟")
    assert next_tier_for_value(750) is None


def test_add_and_leaderboard(store: AffectionStore) -> None:
    r1 = store.add("980229149", "u1", 20, display_name="A")
    assert r1.gained == 20
    assert r1.total == 20
    assert r1.tier_name == "面熟"
    assert r1.tier_upgraded

    store.add("980229149", "u2", 50, display_name="B")
    board = store.leaderboard("980229149", limit=5)
    assert len(board) == 2
    assert board[0].user_id == "u2"


def test_add_chat_unlimited(store: AffectionStore) -> None:
    for _ in range(5):
        r = store.add_chat("g1", "u1", 2)
        assert r.gained == 2
    assert store.get_user("g1", "u1").value == 10


def test_format_gain_footnote_chat_mode() -> None:
    small = AffectionGainResult(gained=2, total=10, tier_name="路人")
    assert format_gain_footnote(small, always_show=False) == ""
    assert "好感 +2" in format_gain_footnote(small)

    upgrade = AffectionGainResult(
        gained=5,
        total=15,
        tier_name="面熟",
        tier_upgraded=True,
    )
    assert "关系升级" in format_gain_footnote(upgrade, always_show=False)


def test_build_affection_brief() -> None:
    brief = build_affection_brief(name="测试", value=42)
    assert "测试" in brief
    assert "常来" in brief
    assert "距" in brief


def test_pick_tier_title_stable_per_user() -> None:
    t1 = pick_tier_title("面熟", user_id="u1", group_id="980229149")
    t2 = pick_tier_title("面熟", user_id="u1", group_id="980229149")
    t3 = pick_tier_title("面熟", user_id="u2", group_id="980229149")
    assert t1 == t2
    assert t1 in AFFECTION_TIER_TITLES["面熟"]
    assert t3 in AFFECTION_TIER_TITLES["面熟"]


def test_tier_title_assigned_on_add(store: AffectionStore) -> None:
    r = store.add("g1", "u1", 5, display_name="A")
    user = store.get_user("g1", "u1")
    assert user.tier_title
    assert user.tier_title in AFFECTION_TIER_TITLES["路人"]
    assert r.title_changed

    r2 = store.add("g1", "u1", 20, display_name="A")
    assert r2.tier_upgraded
    assert r2.title_changed
    user2 = store.get_user("g1", "u1")
    assert user2.tier_title in AFFECTION_TIER_TITLES["面熟"]
    assert user2.tier_title != user.tier_title or len(AFFECTION_TIER_TITLES["面熟"]) == 1


def test_private_affection_scope() -> None:
    scope = affection_scope_private("10001")
    assert scope == "private_10001"
    assert is_private_affection_scope(scope)
    assert not is_private_affection_scope("980229149")


def test_private_affection_store_isolated(store: AffectionStore) -> None:
    private_scope = affection_scope_private("u1")
    store.add(private_scope, "u1", 10, display_name="私聊用户")
    store.add("980229149", "u1", 50, display_name="群用户")
    assert store.get_user(private_scope, "u1").value == 10
    assert store.get_user("980229149", "u1").value == 50


def test_format_help_private_mode() -> None:
    text = format_help(private_mode=True)
    assert "私聊" in text
    assert "分开" in text
