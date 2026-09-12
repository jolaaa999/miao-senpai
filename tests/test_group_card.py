from __future__ import annotations

from dl_senpai.group_card import (
    CardAction,
    CardCandidate,
    CardCooldown,
    filter_actions,
    looks_like_card_request,
    parse_card_actions,
    sanitize_card,
)


def test_parse_card_actions_strips_marker() -> None:
    raw = (
        "好呀，给你改成小太阳啦～\n"
        '<<<CARD {"user_id": 12345, "card": "小太阳", "kind": "request"}>>>'
    )
    parsed = parse_card_actions(raw)
    assert "小太阳啦" in parsed.text
    assert "<<<CARD" not in parsed.text
    assert len(parsed.actions) == 1
    assert parsed.actions[0].user_id == 12345
    assert parsed.actions[0].card == "小太阳"
    assert parsed.actions[0].kind == "request"


def test_parse_card_actions_userId_and_smart_quotes() -> None:
    raw = '<<<CARD {“userId”: 99, “card”: “卷王”, “kind”: “request”}>>>'
    parsed = parse_card_actions(raw)
    assert len(parsed.actions) == 1
    assert parsed.actions[0].user_id == 99
    assert parsed.actions[0].card == "卷王"


def test_sanitize_card() -> None:
    assert sanitize_card("  学弟一号  ", max_len=16) == "学弟一号"
    assert sanitize_card("很长很长很长很长很长很长很长", max_len=6) == "很长很长很长"
    assert sanitize_card("看 https://evil.com", max_len=40) is None
    assert sanitize_card("", max_len=10) is None


def test_filter_actions_respects_role_and_cooldown() -> None:
    candidates = [
        CardCandidate(1, "甲", "", "member", "sender"),
        CardCandidate(2, "乙", "", "admin", "at"),
        CardCandidate(3, "丙", "", "member", "at"),
    ]
    actions = [
        CardAction(1, "甲哥", "request"),
        CardAction(2, "乙管", "request"),
        CardAction(3, "丙宝", "playful"),
    ]
    allowed = filter_actions(
        actions,
        candidates=candidates,
        max_len=16,
        allow_playful=True,
        playful_ready=False,
    )
    assert [a.user_id for a in allowed] == [1]

    allowed2 = filter_actions(
        actions,
        candidates=candidates,
        max_len=16,
        allow_playful=True,
        playful_ready=True,
    )
    assert [a.user_id for a in allowed2] == [1, 3]


def test_filter_actions_allows_admin_sender_self() -> None:
    """管理员本人要求改自己名片时不应被静默丢掉。"""
    candidates = [
        CardCandidate(100, "管管", "旧名", "admin", "sender"),
        CardCandidate(200, "群主", "", "owner", "at"),
    ]
    actions = [
        CardAction(100, "新名", "request"),
        CardAction(200, "乱改群主", "request"),
    ]
    allowed = filter_actions(
        actions,
        candidates=candidates,
        max_len=16,
        allow_playful=True,
        playful_ready=True,
    )
    assert len(allowed) == 1
    assert allowed[0].user_id == 100
    assert allowed[0].card == "新名"


def test_looks_like_card_request() -> None:
    assert looks_like_card_request("学姐把我群名片改成卷王")
    assert looks_like_card_request("给我改个外号")
    assert looks_like_card_request("叫我小太阳")
    assert looks_like_card_request("把他的群名称改成臭猫娘")
    assert looks_like_card_request('把他的群名称改成“臭猫娘”')
    assert looks_like_card_request("@某人 把他群名片改成xxx")
    assert not looks_like_card_request("什么是过拟合")
    assert not looks_like_card_request("不要再改群名称了")
    assert not looks_like_card_request("补药再改群名称了")
    assert not looks_like_card_request("别再改群名片了")
    assert not looks_like_card_request("小猫娘，晚安")
    assert not looks_like_card_request("QWQ")
    assert not looks_like_card_request("你好")


def test_card_cooldown() -> None:
    cd = CardCooldown()
    assert cd.ready("g:1", 60, now=100.0)
    cd.mark("g:1", now=100.0)
    assert not cd.ready("g:1", 60, now=150.0)
    assert cd.ready("g:1", 60, now=161.0)
