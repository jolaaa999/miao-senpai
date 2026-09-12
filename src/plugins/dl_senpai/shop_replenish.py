"""商店缺货时自动爬图识图补货（与 MCP 共用 shop_import 逻辑）。"""

from __future__ import annotations

import asyncio
import random
import time
from typing import TYPE_CHECKING

from nonebot import logger

from .config import PluginConfig, get_config

if TYPE_CHECKING:
    from .shop import ClothingItem, ShopStore

_replenish_locks: dict[str, asyncio.Lock] = {}
_last_replenish: dict[str, float] = {}


def _group_key(group_id: int | str) -> str:
    return str(group_id)


def _lock_for(group_id: int | str) -> asyncio.Lock:
    key = _group_key(group_id)
    if key not in _replenish_locks:
        _replenish_locks[key] = asyncio.Lock()
    return _replenish_locks[key]


def _cooldown_ready(group_id: int | str, cooldown_sec: int) -> bool:
    if cooldown_sec <= 0:
        return True
    key = _group_key(group_id)
    last = _last_replenish.get(key)
    if last is None:
        return True
    return (time.time() - last) >= cooldown_sec


def _mark_replenish(group_id: int | str) -> None:
    _last_replenish[_group_key(group_id)] = time.time()


def needs_auto_replenish(store: ShopStore, group_id: int | str) -> bool:
    state = store.load_state(group_id)
    valid_slots = sum(1 for sid in state.slots if store.catalog.get(sid))
    need = store.slot_count - valid_slots
    if need <= 0:
        return False
    return len(store.catalog.all_items()) < store.slot_count


async def try_auto_replenish(
    group_id: int | str,
    store: ShopStore,
    config: PluginConfig | None = None,
    *,
    rng: random.Random | None = None,
) -> tuple[int, str]:
    """缺货且目录无未收藏款时，自动搜图导入。返回 (成功写入数, 用户可见说明)。"""
    cfg = config or get_config()
    if not cfg.shop_auto_import_enable:
        return 0, ""
    if not needs_auto_replenish(store, group_id):
        return 0, ""
    if not cfg.api_configured():
        return 0, "（目录已空，但未配置识图 API，无法自动补货）"
    if not _cooldown_ready(group_id, cfg.shop_auto_import_cooldown):
        return 0, "（货架补货中，稍后再开商店～）"

    lock = _lock_for(group_id)
    if lock.locked():
        return 0, "（学姐正在给货架上新，稍等一下～）"

    async with lock:
        if not needs_auto_replenish(store, group_id):
            return 0, ""
        if not _cooldown_ready(group_id, cfg.shop_auto_import_cooldown):
            return 0, ""

        rng = rng or random.Random()
        queries = cfg.shop_auto_import_query_list()
        if not queries:
            queries = [
                "洛丽塔连衣裙",
                "JK制服女装",
                "汉服连衣裙",
                "女仆装",
                "哥特萝莉裙",
            ]

        from .shop_import import import_from_search

        per = max(1, min(6, cfg.shop_auto_import_per_run))
        added = 0
        errors: list[str] = []

        # 轮换 1～2 个搜索词，尽量一次补满货架
        order = list(queries)
        rng.shuffle(order)
        for query in order[:2]:
            if added >= per:
                break
            try:
                results = await import_from_search(
                    query,
                    limit=max(1, per - added),
                    generate_preview=False,
                    config=cfg,
                    catalog_path=store.catalog.catalog_path,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"dl_senpai shop auto replenish failed: {exc}")
                errors.append(str(exc)[:60])
                continue
            for r in results:
                if r.ok:
                    added += 1
            if added > 0:
                break

        _mark_replenish(group_id)

        if added > 0:
            store.catalog.reload()
            names = []
            for q in order[:2]:
                names.append(q)
            logger.info(
                f"dl_senpai shop auto replenish group={group_id} added={added}"
            )
            return added, f"📦 货架自动上新 {added} 件（联网搜图 + 识图导入）～"

        hint = "自动补货没成功"
        if errors:
            hint += f"：{errors[0]}"
        else:
            hint += "（可安装 ddgs：python -m pip install ddgs）"
        return 0, f"（{hint}）"


async def ensure_shop_ready(
    group_id: int | str,
    store: ShopStore,
    config: PluginConfig | None = None,
    *,
    rng: random.Random | None = None,
) -> tuple[list[ClothingItem], str]:
    """每日刷新货架；目录耗尽时尝试自动联网补货。"""
    cfg = config or get_config()
    rng = rng or random.Random()
    store.apply_daily_refresh_if_needed(group_id, rng=rng)
    _, items = store.ensure_slots(group_id, rng=rng)
    note = ""
    if needs_auto_replenish(store, group_id):
        added, note = await try_auto_replenish(group_id, store, cfg, rng=rng)
        if added > 0:
            store.apply_daily_refresh_if_needed(group_id, rng=rng)
            _, items = store.ensure_slots(group_id, rng=rng)
    return items, note
