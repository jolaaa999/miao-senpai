from __future__ import annotations

import pytest

from dl_senpai.config import PluginConfig
from dl_senpai.shop import ClothingItem
from dl_senpai.shop_discount import (
    affection_discount_chance,
    quote_shop_item,
    quote_shop_items,
)


def _item() -> ClothingItem:
    return ClothingItem(
        id="blue_lolita",
        name="蓝色洛丽塔",
        price=100,
        outfit_en="blue lolita dress",
        thumb_prompt="thumb",
    )


def _cfg(**overrides) -> PluginConfig:
    base = {
        "shop_affection_discount_enable": True,
        "shop_affection_discount_base_chance": 0.05,
        "shop_affection_discount_chance_per_tier": 0.08,
        "shop_affection_discount_max_chance": 0.75,
        "shop_affection_discount_base_rate": 0.05,
        "shop_affection_discount_rate_per_tier": 0.03,
        "shop_affection_discount_max_rate": 0.35,
    }
    base.update(overrides)
    return PluginConfig(**base)


def test_discount_chance_scales_with_tier() -> None:
    cfg = _cfg()
    assert affection_discount_chance(0, cfg) == 0.05
    assert affection_discount_chance(7, cfg) == pytest.approx(0.61)


def test_quote_stable_for_same_day() -> None:
    cfg = _cfg()
    item = _item()
    q1 = quote_shop_item(
        item,
        group_id="g1",
        user_id="u1",
        affection_value=500,
        refresh_day="2026-08-24",
        config=cfg,
    )
    q2 = quote_shop_item(
        item,
        group_id="g1",
        user_id="u1",
        affection_value=500,
        refresh_day="2026-08-24",
        config=cfg,
    )
    assert q1.sale_price == q2.sale_price
    assert q1.has_discount == q2.has_discount


def test_higher_affection_stronger_discount_when_triggered() -> None:
    cfg = _cfg(
        shop_affection_discount_base_chance=1.0,
        shop_affection_discount_chance_per_tier=0.0,
        shop_affection_discount_max_chance=1.0,
    )
    item = _item()
    low = quote_shop_item(
        item,
        group_id="g1",
        user_id="u1",
        affection_value=0,
        refresh_day="2026-08-24",
        config=cfg,
    )
    high = quote_shop_item(
        item,
        group_id="g1",
        user_id="u1",
        affection_value=700,
        refresh_day="2026-08-24",
        config=cfg,
    )
    assert low.has_discount
    assert high.has_discount
    assert high.discount_percent >= low.discount_percent
    assert high.sale_price <= low.sale_price


def test_no_discount_when_disabled() -> None:
    cfg = _cfg()
    item = _item()
    disabled = quote_shop_item(
        item,
        group_id="g1",
        user_id="u1",
        affection_value=500,
        refresh_day="2026-08-24",
        config=_cfg(shop_affection_discount_enable=False),
    )
    assert not disabled.has_discount
