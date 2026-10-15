"""主动社交：遍历群成员发好友申请。

- 加好友：优先用 NapCat Cookie 调空间 CGI（全自动）；失败再试 OneBot 扩展 API。
  按 group_features 注册表（= 上线同步的全部群）扫成员，每日配额 + 随机间隔。

2026-10-15：名片赞（NapCat send_like）与 QQ 空间动态赞评（onebot-qzone 桥）
按需求整体退役，连带 qzone_client / qzone_sync / qzone_server / qzone_comment
四个模块与相关配置一起删除——赞评是那套桥唯一的使用者，留着就是死代码。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime
from pathlib import Path
from threading import Lock
from zoneinfo import ZoneInfo

try:
    from nonebot import logger
except ImportError:  # 仅单测环境
    import logging

    logger = logging.getLogger("dl_senpai")  # type: ignore[assignment]

from typing import TYPE_CHECKING

from .friend_add_web import send_friend_request_via_web
from .group_features import is_feature_enabled

if TYPE_CHECKING:
    from .config import PluginConfig

_TZ = ZoneInfo("Asia/Shanghai")

_SOCIAL_DIR = Path(__file__).resolve().parents[3] / "data" / "dl_senpai" / "social"

# 加好友申请间随机间隔（秒）：3–8 分钟
_FRIEND_ADD_GAP = (180, 480)
# 后台循环空转间隔（秒）
_LOOP_IDLE = 60

# OneBot 扩展名（NapCat 均不支持；仅作探测，1404 后永久跳过）
_FRIEND_ADD_APIS = (
    "set_buddy_request",
    "add_friend",
    "create_friend_request",
    "friend_request",
)
_friend_add_onebot_dead = False
_locked_add_api: str | None = None
_onebot_unsupported_logged = False
# QQ CGI「服务器繁忙」后冷却到此时间戳（monotonic）再试，避免每分钟空打
_friend_add_busy_until = 0.0
_FRIEND_ADD_BUSY_COOLDOWN = 3600.0


def _today() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d")


class SocialStateStore:
    """当日已发好友申请记录，day-key 翻转自动重置。"""

    def __init__(self, data_dir: str | Path = _SOCIAL_DIR) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def _load(self, name: str) -> dict:
        path = self.dir / name
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _save(self, name: str, data: dict) -> None:
        path = self.dir / name
        tmp = path.with_suffix(".tmp")
        with self._lock:
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(path)

    def sent_today(self) -> dict[str, dict]:
        raw = self._load("friend_requests.json")
        return (
            raw.get("sent")
            if raw.get("day") == _today() and isinstance(raw.get("sent"), dict)
            else {}
        )

    def count_sent_today(self) -> int:
        return len(self.sent_today())

    def record_request(self, user_id: int | str, group_id: int | str) -> None:
        raw = self._load("friend_requests.json")
        day = _today()
        sent = (
            raw.get("sent")
            if raw.get("day") == day and isinstance(raw.get("sent"), dict)
            else {}
        )
        sent[str(user_id)] = {
            "group": str(group_id),
            "ts": datetime.now(_TZ).isoformat(timespec="seconds"),
        }
        self._save("friend_requests.json", {"day": day, "sent": sent})


_store: SocialStateStore | None = None


def get_social_store() -> SocialStateStore:
    global _store
    if _store is None:
        _store = SocialStateStore()
    return _store


def _exclude_ids(config: "PluginConfig") -> frozenset[str]:
    raw = (config.friend_add_exclude or "").strip()
    return frozenset(g.strip() for g in raw.split(",") if g.strip())


async def _get_friend_ids(bot) -> set[str]:
    try:
        friends = await bot.call_api("get_friend_list")
        return {
            str(f.get("user_id"))
            for f in friends or []
            if isinstance(f, dict) and f.get("user_id")
        }
    except Exception:  # noqa: BLE001
        logger.warning("dl_senpai social: get_friend_list failed")
        return set()


async def _list_scan_group_ids(bot, config: "PluginConfig") -> list[str]:
    """全部要扫的群：注册表优先；空则现场 get_group_list。"""
    from .group_features import get_group_feature_store

    gids = [g["group_id"] for g in get_group_feature_store().list_groups()]
    if gids:
        return gids
    try:
        groups = await bot.call_api("get_group_list")
    except Exception:  # noqa: BLE001
        logger.warning("dl_senpai social: get_group_list failed")
        return []
    out: list[str] = []
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("group_id") or "").strip()
        if gid:
            out.append(gid)
    return out


async def _send_friend_request_onebot(
    bot, config: "PluginConfig", user_id: int, group_id: int | str
) -> bool:
    """探测 OneBot 扩展加好友；NapCat 会 1404，探测失败后本进程内不再试。"""
    global _locked_add_api, _friend_add_onebot_dead, _onebot_unsupported_logged
    if _friend_add_onebot_dead:
        return False
    msg = (config.friend_add_verify_msg or "").strip() or "我是群里的学姐~"
    param_variants = (
        {"user_id": user_id, "group_id": int(group_id), "message": msg},
        {"user_id": user_id, "message": msg},
    )
    candidates = (_locked_add_api,) if _locked_add_api else _FRIEND_ADD_APIS
    saw_unsupported = False
    last_err: BaseException | None = None
    for api in candidates:
        for kwargs in param_variants:
            try:
                await bot.call_api(api, **kwargs)
                _locked_add_api = api
                return True
            except Exception as e:  # noqa: BLE001
                last_err = e
                err = str(e)
                if "1404" in err or "不支持的Api" in err or "不支持的 API" in err:
                    saw_unsupported = True
                break  # 同一 api 换参数多半也挂，换下一个 api
        if saw_unsupported:
            break
    _friend_add_onebot_dead = True
    if not _onebot_unsupported_logged:
        _onebot_unsupported_logged = True
        logger.warning(
            "dl_senpai social: 当前协议端不支持主动加好友 OneBot API"
            "（如 NapCat 的 friend_request/add_friend），已改走 Cookie CGI，不再重试 OneBot。"
            f" last_error={last_err}"
        )
    return False


async def _send_friend_request(
    bot, config: "PluginConfig", user_id: int, group_id: int | str
) -> tuple[bool, str]:
    """全自动加好友：默认 Cookie CGI；auto 时 OneBot 仅探测一次。返回 (ok, detail)。"""
    mode = (getattr(config, "friend_add_mode", "web") or "web").strip().lower()
    msg = (config.friend_add_verify_msg or "").strip() or "我是群里的学姐~"
    self_id = getattr(bot, "self_id", "") or ""

    if mode in ("auto", "web", "cookie"):
        ok, detail = await send_friend_request_via_web(
            bot,
            self_id=self_id,
            user_id=user_id,
            group_id=group_id,
            verify_msg=msg,
        )
        if ok:
            logger.info(
                f"dl_senpai social: web friend add ok uid={user_id} group={group_id}"
            )
            return True, detail
        logger.warning(
            f"dl_senpai social: web friend add failed uid={user_id} group={group_id}: {detail}"
        )
        if mode in ("web", "cookie"):
            return False, detail

    if mode in ("auto", "onebot") and not _friend_add_onebot_dead:
        ok = await _send_friend_request_onebot(bot, config, user_id, group_id)
        return ok, "onebot" if ok else "onebot_failed"
    return False, "unsupported"


async def friend_add_tick(bot, config: "PluginConfig") -> int:
    """扫一轮群成员发申请，返回本轮实际发出数。"""
    global _friend_add_busy_until
    if not config.friend_add_enable:
        return 0
    now_m = time.monotonic()
    if now_m < _friend_add_busy_until:
        return 0
    store = get_social_store()
    quota = max(0, int(config.friend_add_daily_limit) - store.count_sent_today())
    if quota == 0:
        return 0
    friends = await _get_friend_ids(bot)
    excludes = _exclude_ids(config)
    self_id = str(getattr(bot, "self_id", "") or "")
    sent = 0

    gids = await _list_scan_group_ids(bot, config)
    for gid in gids:
        if sent >= quota:
            break
        if not is_feature_enabled(config, gid, "friend_add"):
            continue
        try:
            members = await bot.call_api("get_group_member_list", group_id=int(gid))
        except Exception:  # noqa: BLE001
            logger.warning(f"dl_senpai social: get_group_member_list failed group={gid}")
            continue
        # 打乱顺序，避免总从前排加
        rows = [m for m in (members or []) if isinstance(m, dict)]
        random.shuffle(rows)
        for m in rows:
            if sent >= quota:
                break
            uid = str(m.get("user_id") or "")
            if not uid or uid == self_id or uid in friends or uid in excludes:
                continue
            if uid in store.sent_today():
                continue
            ok, detail = await _send_friend_request(bot, config, int(uid), gid)
            if ok:
                store.record_request(uid, gid)
                sent += 1
                logger.info(
                    f"dl_senpai social: friend request sent uid={uid} group={gid} "
                    f"({store.count_sent_today()}/{config.friend_add_daily_limit})"
                )
                await asyncio.sleep(random.uniform(*_FRIEND_ADD_GAP))
            else:
                # QQ CGI -99997「服务器繁忙」多半是接口拒/风控，冷却后再试
                if "服务器繁忙" in detail or "-99997" in detail or "-5011" in detail:
                    _friend_add_busy_until = time.monotonic() + _FRIEND_ADD_BUSY_COOLDOWN
                    logger.warning(
                        "dl_senpai social: friend_add cooldown "
                        f"{int(_FRIEND_ADD_BUSY_COOLDOWN)}s (QQ busy/blocked)"
                    )
                    return sent
                await asyncio.sleep(random.uniform(2.0, 6.0))
    return sent


async def social_loop(bot, config: "PluginConfig | None" = None) -> None:
    """后台主循环。"""
    from .config import get_config

    logger.info("[dl_senpai] social loop started")
    while True:
        cfg = config if config is not None else get_config()
        try:
            sent = await friend_add_tick(bot, cfg)
            if sent:
                logger.info(f"dl_senpai social: friend add tick sent={sent}")
        except Exception:  # noqa: BLE001
            logger.exception("dl_senpai social: friend add tick crashed")
        await asyncio.sleep(_LOOP_IDLE)
