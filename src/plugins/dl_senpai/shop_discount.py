from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .affection import tier_for_value, tier_index_for_value
from .config import PluginConfig
from .shop import ClothingItem


@dataclass(frozen=True)
class ShopItemQuote:
    item: ClothingItem
    list_price: int
    sale_price: int
    discount_percent: int = 0

    @property
    def has_discount(self) -> bool:
        return self.sale_price < self.list_price


def _stable_roll(group_id: int | str, user_id: int | str, item_id: str, refresh_day: str) -> float:
    seed = f"{group_id}:{user_id}:{item_id}:{refresh_day}".encode()
    digest = hashlib.md5(seed).hexdigest()
    return int(digest[:8], 16) / 0xFFFFFFFF


def _stable_bonus_rate(group_id: int | str, user_id: int | str, item_id: str, refresh_day: str) -> float:
    seed = f"rate:{group_id}:{user_id}:{item_id}:{refresh_day}".encode()
    digest = hashlib.md5(seed).hexdigest()
    return (int(digest[8:12], 16) % 5) / 100.0


def affection_discount_chance(tier_index: int, config: PluginConfig) -> float:
    chance = (
        config.shop_affection_discount_base_chance
        + tier_index * config.shop_affection_discount_chance_per_tier
    )
    return min(config.shop_affection_discount_max_chance, max(0.0, chance))


def affection_discount_rate(tier_index: int, config: PluginConfig, *, bonus: float = 0.0) -> float:
    rate = (
        config.shop_affection_discount_base_rate
        + tier_index * config.shop_affection_discount_rate_per_tier
        + bonus
    )
    return min(config.shop_affection_discount_max_rate, max(0.0, rate))


def quote_shop_item(
    item: ClothingItem,
    *,
    group_id: int | str,
    user_id: int | str,
    affection_value: int | None,
    refresh_day: str,
    config: PluginConfig,
) -> ShopItemQuote:
    list_price = item.price
    if not config.shop_affection_discount_enable or affection_value is None:
        return ShopItemQuote(
            item=item,
            list_price=list_price,
            sale_price=list_price,
            discount_percent=0,
        )

    tier_idx = tier_index_for_value(affection_value)
    chance = affection_discount_chance(tier_idx, config)
    roll = _stable_roll(group_id, user_id, item.id, refresh_day)
    if roll >= chance:
        return ShopItemQuote(
            item=item,
            list_price=list_price,
            sale_price=list_price,
            discount_percent=0,
        )

    bonus = _stable_bonus_rate(group_id, user_id, item.id, refresh_day)
    rate = affection_discount_rate(tier_idx, config, bonus=bonus)
    sale_price = max(1, int(round(list_price * (1.0 - rate))))
    if sale_price >= list_price:
        return ShopItemQuote(
            item=item,
            list_price=list_price,
            sale_price=list_price,
            discount_percent=0,
        )
    discount_percent = max(1, round((1.0 - sale_price / list_price) * 100))
    return ShopItemQuote(
        item=item,
        list_price=list_price,
        sale_price=sale_price,
        discount_percent=discount_percent,
    )


def quote_shop_items(
    items: list[ClothingItem],
    *,
    group_id: int | str,
    user_id: int | str,
    affection_value: int | None,
    refresh_day: str,
    config: PluginConfig,
) -> list[ShopItemQuote]:
    return [
        quote_shop_item(
            item,
            group_id=group_id,
            user_id=user_id,
            affection_value=affection_value,
            refresh_day=refresh_day,
            config=config,
        )
        for item in items
    ]


def format_quote_price_line(quote: ShopItemQuote) -> str:
    if quote.has_discount:
        return (
            f"{quote.item.name} · 原价 {quote.list_price} 分 → "
            f"特惠 {quote.sale_price} 分（-{quote.discount_percent}%）"
        )
    return f"{quote.item.name} · {quote.list_price} 分"


def format_discount_hint(affection_value: int | None, config: PluginConfig) -> str:
    if not config.shop_affection_discount_enable or affection_value is None:
        return ""
    _threshold, tier_name, _hint = tier_for_value(affection_value)
    tier_idx = tier_index_for_value(affection_value)
    chance = int(round(affection_discount_chance(tier_idx, config) * 100))
    max_rate = int(round(affection_discount_max_rate_for_tier(tier_idx, config) * 100))
    return (
        f"💝 你是「{tier_name}」档，今日货架好感特惠概率约 {chance}%，"
        f"最高可省 {max_rate}%"
    )


def affection_discount_max_rate_for_tier(tier_index: int, config: PluginConfig) -> float:
    return affection_discount_rate(tier_index, config, bonus=0.04)
