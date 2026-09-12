from __future__ import annotations

from dl_senpai.group_card import CardCandidate
from dl_senpai.group_mute import (
    MuteAction,
    can_emit_mute,
    filter_mute_actions,
    looks_like_mute_request,
    looks_like_provocation,
    parse_mute_actions,
)
from dl_senpai.style import polish_reply


def test_parse_mute_actions() -> None:
    raw = (
        "哼，先安静一会儿！\n"
        '<<<MUTE {"user_id": 42, "duration": 90, "kind": "angry"}>>>'
    )
    parsed = parse_mute_actions(raw)
    assert "<<<MUTE" not in parsed.text
    assert "哼" in parsed.text
    assert len(parsed.actions) == 1
    assert parsed.actions[0].user_id == 42
    assert parsed.actions[0].duration == 90


def test_can_emit_mute_gates() -> None:
    assert looks_like_provocation("你就是个废物")
    assert looks_like_mute_request("帮我禁言他一下")
    assert can_emit_mute("学姐你是废物吧")
    assert can_emit_mute("禁言这个人")
    assert not can_emit_mute("小猫娘，晚安")
    assert not can_emit_mute("QWQ")
    assert not can_emit_mute("你好")


def test_filter_mute_actions() -> None:
    candidates = [
        CardCandidate(1, "甲", "", "member", "sender"),
        CardCandidate(2, "乙", "", "admin", "at"),
    ]
    actions = [
        MuteAction(1, 999, "angry"),
        MuteAction(2, 60, "angry"),
    ]
    allowed, reason = filter_mute_actions(
        actions,
        candidates=candidates,
        max_duration=120,
        min_duration=30,
        ready=True,
    )
    assert reason == ""
    assert len(allowed) == 1
    assert allowed[0].user_id == 1
    assert allowed[0].duration == 120

    blocked, reason2 = filter_mute_actions(
        actions,
        candidates=candidates,
        max_duration=120,
        min_duration=30,
        ready=False,
    )
    assert blocked == []
    assert reason2 == "cooldown"

    only_admin, reason3 = filter_mute_actions(
        [MuteAction(2, 60, "request")],
        candidates=candidates,
        max_duration=120,
        min_duration=30,
        ready=True,
    )
    assert only_admin == []
    assert reason3 == "admin"


def test_resolve_explicit_mute_self() -> None:
    from dl_senpai.group_mute import resolve_explicit_mute

    candidates = [
        CardCandidate(100, "我", "", "member", "sender"),
        CardCandidate(200, "管", "", "admin", "at"),
    ]
    action, reason = resolve_explicit_mute(
        "禁言我",
        sender_id=100,
        at_ids=[],
        candidates=candidates,
        duration=60,
    )
    assert reason == ""
    assert action is not None
    assert action.user_id == 100

    action2, reason2 = resolve_explicit_mute(
        "禁言我",
        sender_id=200,
        at_ids=[],
        candidates=[CardCandidate(200, "管", "", "admin", "sender")],
        duration=60,
    )
    assert action2 is None
    assert reason2 == "admin"


def test_polish_strips_card_and_fake_logs() -> None:
    text = polish_reply(
        "晚安～\n"
        "[臭臭的群名片变为「晚安」]\n"
        '[<<<CARD {"user_id":1,"card":"晚安","kind":"request"}>>>]\n'
        "明天见"
    )
    assert "<<<CARD" not in text
    assert "群名片变为" not in text
    assert "晚安" in text
    assert "明天见" in text
