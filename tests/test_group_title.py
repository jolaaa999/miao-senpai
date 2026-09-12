from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from dl_senpai.group_card import (
    TitleApplyResult,
    _card_safe_for_managed_title,
    apply_group_title,
    title_apply_success_note,
)


def test_card_safe_for_managed_title() -> None:
    known = frozenset({"小甜心", "打卡达人"})
    assert _card_safe_for_managed_title("", known_titles=known)
    assert _card_safe_for_managed_title("小甜心", known_titles=known)
    assert not _card_safe_for_managed_title("我自己起的名", known_titles=known)


def test_title_apply_success_note() -> None:
    assert "专属头衔" in title_apply_success_note(
        TitleApplyResult(ok=True, method="special", title="小甜心", verified=True)
    )
    assert "群名片" in title_apply_success_note(
        TitleApplyResult(ok=True, method="card", title="小甜心", verified=True)
    )


@pytest.mark.asyncio
async def test_apply_group_title_special_verified(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = AsyncMock()
    monkeypatch.setattr(
        "dl_senpai.group_card._try_special_title",
        AsyncMock(return_value=True),
    )
    result = await apply_group_title(
        bot,
        group_id=1,
        user_id=2,
        title="小甜心",
        allow_card_fallback=False,
    )
    assert result == TitleApplyResult(
        ok=True,
        method="special",
        title="小甜心",
        verified=True,
    )


@pytest.mark.asyncio
async def test_apply_group_title_card_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    bot = AsyncMock()
    monkeypatch.setattr(
        "dl_senpai.group_card._try_special_title",
        AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "dl_senpai.group_card._read_card",
        AsyncMock(side_effect=["", "小甜心"]),
    )
    result = await apply_group_title(
        bot,
        group_id=1,
        user_id=2,
        title="小甜心",
        allow_card_fallback=True,
        known_managed_titles=frozenset({"小甜心"}),
    )
    assert result.method == "card"
    assert result.verified is True
