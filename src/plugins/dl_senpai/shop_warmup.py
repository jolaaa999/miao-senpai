
"""商店素材后台预热。

为什么需要：货架每天凌晨 4 点换一批新衣服，缩略图是首次访问时才生成的，
单张约 85 秒 —— 用户早上第一句「商店」就要等 4 分半（实测 4 分 19 秒），
很容易被当成 bot 挂了。后台按注册群提前画好，白天访问即命中缓存。

为什么用循环而不是定时器：机器人可能中途重启、也会被热重载打断，
循环里每轮重新读配置和群列表，重启后能自愈。
"""

from __future__ import annotations

import asyncio

from nonebot import logger

from .config import get_config
from .group_features import get_group_feature_store, is_feature_enabled
from .shop import get_shop_store, prewarm_shop_assets
from .shop_replenish import ensure_shop_ready

# 首轮延迟：错开启动时的注册/同步动作，避免和群列表刷新抢 IO
_WARMUP_FIRST_DELAY = 60
# 轮询间隔：覆盖「凌晨 4 点换新」这一批，6 小时足够且不浪费额度
_WARMUP_INTERVAL = 6 * 3600
# 单群之间的间隔，避免所有群同时打绘图接口
_WARMUP_GROUP_GAP = 5


async def warmup_once() -> tuple[int, int]:
    """遍历已注册群，把当日货架缺的缩略图补齐。返回 (成功群数, 生成张数)。"""
    cfg = get_config()
    if not getattr(cfg, "shop_warmup", True) or not cfg.shop_enable:
        return 0, 0
    store_obj = get_shop_store(cfg.shop_dir)
    groups = get_group_feature_store().list_groups()
    ok_groups = 0
    total_ok = 0
    for entry in groups:
        gid = entry.get("group_id")
        if not gid:
            continue
        # 群级开关关掉商店的群不预热，省额度
        if not is_feature_enabled(cfg, gid, "shop"):
            continue
        try:
            items, _note = await ensure_shop_ready(gid, store_obj, cfg)
            if not items:
                continue
            made = await prewarm_shop_assets(items, store_obj, cfg)
            total_ok += made
            ok_groups += 1
        except Exception as exc:  # noqa: BLE001
            # 单群失败（限流、目录损坏）不能中断整轮预热
            logger.warning(f"dl_senpai shop warmup group={gid} failed: {exc}")
        await asyncio.sleep(_WARMUP_GROUP_GAP)
    return ok_groups, total_ok


async def shop_warmup_loop(bot=None) -> None:
    """后台循环：启动 60 秒后跑首轮，之后每 6 小时一轮。"""
    await asyncio.sleep(_WARMUP_FIRST_DELAY)
    while True:
        try:
            if get_config().shop_warmup:
                groups, made = await warmup_once()
                if made or groups:
                    logger.info(
                        f"dl_senpai shop warmup round done groups={groups} images={made}"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"dl_senpai shop warmup round failed: {exc}")
        await asyncio.sleep(_WARMUP_INTERVAL)
