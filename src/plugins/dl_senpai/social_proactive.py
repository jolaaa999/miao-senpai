"""主动社交：遍历群成员发好友申请 + 名片赞 + QQ 空间动态赞评。

- 加好友：优先用 NapCat Cookie 调空间 CGI（全自动）；失败再试 OneBot 扩展 API。
  按 group_features 注册表（= 上线同步的全部群）扫成员，每日配额 + 随机间隔。
- 名片赞：北京时间过 like_daily_hour 后 send_like。
- 空间：对接 onebot-qzone HTTP 桥（A 方案）拉好友动态并点赞/评论。
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
from .qzone_client import QZoneBridgeClient, QZoneBridgeError, normalize_feeds
from .qzone_comment import generate_qzone_comment

if TYPE_CHECKING:
    from .config import PluginConfig

_TZ = ZoneInfo("Asia/Shanghai")

_SOCIAL_DIR = Path(__file__).resolve().parents[3] / "data" / "dl_senpai" / "social"

# 加好友申请间随机间隔（秒）：3–8 分钟
_FRIEND_ADD_GAP = (180, 480)
# 名片赞间随机间隔（秒）：5–15
_LIKE_GAP = (5, 15)
# 空间互动间隔（秒）
_QZONE_GAP = (8, 25)
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
_qzone_cookie_synced = False
_onebot_unsupported_logged = False
# QQ CGI「服务器繁忙」后冷却到此时间戳（monotonic）再试，避免每分钟空打
_friend_add_busy_until = 0.0
_FRIEND_ADD_BUSY_COOLDOWN = 3600.0


def _today() -> str:
    return datetime.now(_TZ).strftime("%Y-%m-%d")


class SocialStateStore:
    """当日已发申请 / 名片赞 / 空间互动记录，day-key 翻转自动重置。"""

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

    def liked_today(self) -> set[str]:
        raw = self._load("likes.json")
        if raw.get("day") == _today() and isinstance(raw.get("liked"), list):
            return {str(u) for u in raw["liked"]}
        return set()

    def record_like(self, user_id: int | str) -> None:
        raw = self._load("likes.json")
        day = _today()
        liked = (
            raw.get("liked")
            if raw.get("day") == day and isinstance(raw.get("liked"), list)
            else []
        )
        uid = str(user_id)
        if uid not in liked:
            liked.append(uid)
        self._save("likes.json", {"day": day, "liked": liked})

    def qzone_done_today(self) -> dict[str, list]:
        raw = self._load("qzone.json")
        if raw.get("day") != _today():
            return {"liked": [], "commented": []}
        liked = raw.get("liked") if isinstance(raw.get("liked"), list) else []
        commented = raw.get("commented") if isinstance(raw.get("commented"), list) else []
        return {"liked": [str(x) for x in liked], "commented": [str(x) for x in commented]}

    def record_qzone(self, *, liked_tid: str | None = None, commented_tid: str | None = None) -> None:
        raw = self._load("qzone.json")
        day = _today()
        done = self.qzone_done_today() if raw.get("day") == day else {"liked": [], "commented": []}
        if liked_tid and liked_tid not in done["liked"]:
            done["liked"].append(str(liked_tid))
        if commented_tid and commented_tid not in done["commented"]:
            done["commented"].append(str(commented_tid))
        self._save("qzone.json", {"day": day, **done})


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


def like_hour_reached(config: "PluginConfig", now: datetime | None = None) -> bool:
    now = now or datetime.now(_TZ)
    return now.astimezone(_TZ).hour >= max(0, min(23, int(config.like_daily_hour)))


async def like_tick(bot, config: "PluginConfig") -> int:
    """名片赞：给当日未点赞的好友 send_like。"""
    if not config.like_enable:
        return 0
    if not like_hour_reached(config):
        return 0
    store = get_social_store()
    liked = store.liked_today()
    friends = await _get_friend_ids(bot)
    self_id = str(getattr(bot, "self_id", "") or "")
    times = max(1, min(10, int(config.like_times)))
    count = 0
    for uid in sorted(friends):
        if not uid or uid == self_id or uid in liked:
            continue
        try:
            await bot.call_api("send_like", user_id=int(uid), times=times)
        except Exception as e:  # noqa: BLE001
            err = str(e)
            # 自己出现在好友列表时跳过，避免每分钟刷「禁止给自己点赞」
            if "禁止给自己点赞" in err or uid == self_id:
                store.record_like(uid)
                continue
            logger.warning(f"dl_senpai social: send_like failed uid={uid}: {e}")
            continue
        store.record_like(uid)
        count += 1
        if count % 20 == 0:
            logger.info(f"dl_senpai social: liked {count} friends today")
        await asyncio.sleep(random.uniform(*_LIKE_GAP))
    if count:
        logger.info(
            f"dl_senpai social: like tick done, {count} new + {len(liked)} already"
        )
    return count


def _qzone_client(config: "PluginConfig") -> QZoneBridgeClient:
    return QZoneBridgeClient(
        getattr(config, "qzone_bridge_url", "") or "",
        access_token=getattr(config, "qzone_access_token", "") or "",
        timeout=float(getattr(config, "qzone_timeout", 30) or 30),
    )


def _comment_templates(config: "PluginConfig") -> list[str]:
    raw = getattr(config, "qzone_comment_templates", "") or ""
    parts = [p.strip() for p in raw.replace("，", ",").split(",") if p.strip()]
    return parts or ["好看！", "冲！", "学姐路过点个赞~"]


async def _sync_qzone_cookie(bot, client: QZoneBridgeClient) -> None:
    global _qzone_cookie_synced
    if _qzone_cookie_synced:
        return
    try:
        from .config import get_config
        from .qzone_sync import sync_napcat_cookies_to_qzone

        await sync_napcat_cookies_to_qzone(bot, get_config())
        _qzone_cookie_synced = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"dl_senpai social: qzone cookie sync skipped: {exc}")
        # 仍尝试旧路径：仅 login_cookie
        try:
            data = await bot.call_api("get_cookies", domain="qzone.qq.com")
            cookie = ""
            if isinstance(data, dict):
                cookie = str(data.get("cookies") or "").strip()
            if cookie:
                await client.login_cookie(cookie)
                _qzone_cookie_synced = True
                logger.info("dl_senpai social: qzone bridge cookie synced (fallback)")
        except Exception as exc2:  # noqa: BLE001
            logger.warning(f"dl_senpai social: qzone cookie fallback failed: {exc2}")


async def qzone_tick(bot, config: "PluginConfig") -> dict[str, int]:
    """刷好友动态：点赞 + 可选评论。依赖 onebot-qzone 桥。"""
    result = {"liked": 0, "commented": 0}
    if not getattr(config, "qzone_enable", False):
        return result
    qh = getattr(config, "qzone_daily_hour", None)
    hour = int(config.like_daily_hour if qh is None else qh)
    now = datetime.now(_TZ)
    if now.astimezone(_TZ).hour < max(0, min(23, hour)):
        return result

    client = _qzone_client(config)
    if not client.configured():
        logger.warning("dl_senpai social: qzone_enable but bridge url empty")
        return result

    if getattr(config, "qzone_sync_cookie", True):
        await _sync_qzone_cookie(bot, client)

    store = get_social_store()
    done = store.qzone_done_today()
    like_quota = max(
        0, int(getattr(config, "qzone_like_daily_limit", 30)) - len(done["liked"])
    )
    comment_quota = max(
        0,
        int(getattr(config, "qzone_comment_daily_limit", 10)) - len(done["commented"]),
    )
    do_like = bool(getattr(config, "qzone_like_enable", True)) and like_quota > 0
    do_comment = (
        bool(getattr(config, "qzone_comment_enable", True)) and comment_quota > 0
    )
    if not do_like and not do_comment:
        return result

    try:
        raw = await client.get_friend_feeds(
            num=max(5, min(50, int(getattr(config, "qzone_feed_num", 20) or 20))),
            include_image_data=False,
        )
    except QZoneBridgeError as exc:
        logger.warning(f"dl_senpai social: get_friend_feeds failed: {exc}")
        return result

    feeds = normalize_feeds(raw)
    self_id = str(getattr(bot, "self_id", "") or "")

    for feed in feeds:
        if like_quota <= 0 and comment_quota <= 0:
            break
        uid, tid = feed["user_id"], feed["tid"]
        if uid == self_id:
            continue
        key = f"{uid}:{tid}"

        if do_like and like_quota > 0 and key not in done["liked"] and not feed.get("liked"):
            try:
                await client.like_feed(uid, tid, feed.get("abstime") or "")
                store.record_qzone(liked_tid=key)
                done["liked"].append(key)
                like_quota -= 1
                result["liked"] += 1
                logger.info(f"dl_senpai social: qzone liked {key}")
            except QZoneBridgeError as exc:
                logger.warning(f"dl_senpai social: qzone like failed {key}: {exc}")
            await asyncio.sleep(random.uniform(*_QZONE_GAP))

        if do_comment and comment_quota > 0 and key not in done["commented"]:
            # 低概率评论，避免刷屏：默认约 35%
            if random.random() > float(getattr(config, "qzone_comment_prob", 0.35) or 0.35):
                continue
            summary = str(feed.get("summary") or "")
            nick = str(feed.get("nickname") or "")
            try:
                text = await generate_qzone_comment(
                    config, summary=summary, nickname=nick
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"dl_senpai social: qzone comment gen failed {key}: {exc}")
                text = random.choice(_comment_templates(config))
            if not (text or "").strip():
                continue
            try:
                await client.comment_feed(uid, tid, text.strip())
                store.record_qzone(commented_tid=key)
                done["commented"].append(key)
                comment_quota -= 1
                result["commented"] += 1
                logger.info(
                    f"dl_senpai social: qzone commented {key} "
                    f"text={text!r} feed={summary[:40]!r}"
                )
            except QZoneBridgeError as exc:
                logger.warning(f"dl_senpai social: qzone comment failed {key}: {exc}")
            await asyncio.sleep(random.uniform(*_QZONE_GAP))

    if result["liked"] or result["commented"]:
        logger.info(
            f"dl_senpai social: qzone tick liked={result['liked']} "
            f"commented={result['commented']}"
        )
    return result


async def social_loop(bot, config: "PluginConfig | None" = None) -> None:
    """后台主循环。"""
    from .config import get_config

    logger.info("[dl_senpai] social loop started")
    while True:
        cfg = config if config is not None else get_config()
        try:
            liked = await like_tick(bot, cfg)
            if liked:
                logger.info(f"dl_senpai social: like tick liked={liked}")
        except Exception:  # noqa: BLE001
            logger.exception("dl_senpai social: like tick crashed")
        try:
            qz = await qzone_tick(bot, cfg)
            if qz.get("liked") or qz.get("commented"):
                logger.info(f"dl_senpai social: qzone tick {qz}")
        except Exception:  # noqa: BLE001
            logger.exception("dl_senpai social: qzone tick crashed")
        try:
            sent = await friend_add_tick(bot, cfg)
            if sent:
                logger.info(f"dl_senpai social: friend add tick sent={sent}")
        except Exception:  # noqa: BLE001
            logger.exception("dl_senpai social: friend add tick crashed")
        await asyncio.sleep(_LOOP_IDLE)
