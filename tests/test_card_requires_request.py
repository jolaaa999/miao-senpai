"""改名片的准入闸门：没人叫她改，就不许改。

为什么单独测：名片是"别人看得见"的副作用，而且 agent 工具这条路径不经过
`group_card.filter_actions()`，之前没有任何"这次到底有没有被要求"的判定——
模型一时兴起写个 <<<CARD ...>>> 就直接改。这类回归不会报错，只会让人觉得她乱来。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dl_senpai.agent.tools.executors import execute_card
from dl_senpai.agent.tools.models import ToolCall


class _FakeBot:
    def __init__(self) -> None:
        self.cards: list[dict] = []

    async def set_group_card(self, *, group_id: int, user_id: int, card: str):
        self.cards.append({"group_id": group_id, "user_id": user_id, "card": card})

    async def get_group_member_info(self, *, group_id: int, user_id: int, no_cache: bool = False):
        hit = self.cards[-1] if self.cards else None
        return {"card": (hit or {}).get("card", "")}


def _cfg(**over: object) -> SimpleNamespace:
    base = dict(card_require_request=True, card_max_len=16)
    base.update(over)
    return SimpleNamespace(**base)


def _ctx(text: str, *, cfg=None, bot=None) -> SimpleNamespace:
    return SimpleNamespace(
        plain_text=text,
        config=cfg or _cfg(),
        bot=bot or _FakeBot(),
        group_id="100",
        extra={},
    )


def _call(*, card: str = "小明", kind: str = "request", user_id: int = 123) -> ToolCall:
    return ToolCall(
        id="t1", tool_name="card",
        arguments={"user_id": user_id, "card": card, "kind": kind},
    )


@pytest.mark.asyncio
async def test_card_rejected_when_nobody_asked() -> None:
    """闲聊里她自己想改：拦住，且一次接口都不该调。"""
    ctx = _ctx("晚安啦，今天也辛苦你了")
    result = await execute_card(ctx, _call())
    assert result.success is False
    assert result.error_type == "not_requested"
    assert ctx.bot.cards == []


@pytest.mark.asyncio
async def test_card_applied_when_explicitly_asked() -> None:
    """对方明确要求：正常改。"""
    ctx = _ctx("把我名片改成小明吧")
    result = await execute_card(ctx, _call(card="小明"))
    assert result.success is True, result.error
    assert ctx.bot.cards == [{"group_id": 100, "user_id": 123, "card": "小明"}]


@pytest.mark.asyncio
async def test_card_rejected_for_playful_kind() -> None:
    """kind=playful 一律拒：取消"自己觉得好玩就改"。"""
    ctx = _ctx("把我名片改成小明吧")
    result = await execute_card(ctx, _call(kind="playful"))
    assert result.success is False
    assert result.error_type == "not_requested"
    assert ctx.bot.cards == []


@pytest.mark.asyncio
async def test_card_rejected_when_negated() -> None:
    """「不要再改我名片了」是拒绝，不是请求。"""
    ctx = _ctx("不要再改我名片了")
    result = await execute_card(ctx, _call())
    assert result.success is False
    assert result.error_type == "not_requested"
    assert ctx.bot.cards == []


@pytest.mark.asyncio
async def test_card_switch_off_falls_back_to_loose() -> None:
    """开关关掉 = 回退旧行为：不检查这句话有没有提改名片。"""
    ctx = _ctx("晚安啦", cfg=_cfg(card_require_request=False))
    result = await execute_card(ctx, _call())
    assert result.success is True, result.error
    assert len(ctx.bot.cards) == 1


@pytest.mark.asyncio
async def test_card_rejects_banned_content() -> None:
    """违禁词/空内容直接拒（sanitize_card 与 legacy 对齐）。"""
    ctx = _ctx("把我名片改成 https://spam.example")
    result = await execute_card(ctx, _call(card="https://spam.example"))
    assert result.success is False
    assert result.error_type == "bad_card"
    assert ctx.bot.cards == []


@pytest.mark.asyncio
async def test_card_truncates_to_max_len() -> None:
    """超长名片按配置截断，而不是被拒。"""
    ctx = _ctx("把我名片改成很长很长的名字", cfg=_cfg(card_max_len=4))
    result = await execute_card(ctx, _call(card="很长很长的名字"))
    assert result.success is True, result.error
    assert ctx.bot.cards[0]["card"] == "很长很长"
