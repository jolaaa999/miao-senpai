"""按群功能开关：三态覆盖（缺省跟随 .env 全局总开关）。

数据文件 data/dl_senpai/admin/group_features.json 由本模块与 Go 控制台
（admin/backend）共享读写；bot 侧带 mtime 缓存，控制台改动无需重启即生效。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

try:
    from nonebot import logger
except ImportError:  # 仅单测环境（无 nonebot driver）
    import logging

    logger = logging.getLogger("dl_senpai")  # type: ignore[assignment]

if TYPE_CHECKING:
    from .config import PluginConfig

# 功能目录：(key, 中文名)。key 与 config.py 的 {key}_enable 字段一一对应。
FEATURES: tuple[tuple[str, str], ...] = (
    ("checkin", "签到"),
    ("affection", "好感度"),
    ("shop", "积分商店"),
    ("draw", "生图"),
    ("card", "卡片图回复"),
    ("mute", "生气禁言"),
    ("browser", "联网浏览"),
    ("search", "网络搜索"),
    ("sticker", "表情包"),
    ("voice", "语音回复"),
    ("welcome", "入群欢迎"),
    ("verify", "进群认证"),
    ("group_style", "群风格"),
    ("memory_recall", "记忆回想"),
    ("person_memory", "个人记忆"),
    ("inner_state", "内心状态"),
    ("vision", "识图"),
    ("trends", "热点趋势"),
    ("friend_add", "自动加好友"),
    ("like", "每日点赞"),
    ("qzone", "空间动态赞评"),
)
FEATURE_KEYS = frozenset(k for k, _ in FEATURES)

# 群名刷新节流（秒）
_NAME_REFRESH_SEC = 86400


def default_group_features_path(project_root: str | Path | None = None) -> Path:
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[3]
    return root / "data" / "dl_senpai" / "admin" / "group_features.json"


class GroupFeatureStore:
    """群注册表 + 每群功能覆盖。features 里缺失的 key = 跟随全局。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._cache: dict | None = None
        self._mtime: float | None = None

    # -- 读 --

    def _load(self) -> dict:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if self._cache is not None and self._mtime == mtime:
            return self._cache
        raw: dict = {}
        if mtime is not None:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw = loaded
            except (OSError, json.JSONDecodeError):
                logger.warning(f"dl_senpai group_features: failed to read {self.path}")
        groups = raw.get("groups")
        if not isinstance(groups, dict):
            groups = {}
        raw["version"] = 1
        raw["groups"] = groups
        self._cache = raw
        self._mtime = mtime
        return raw

    def _save(self, data: dict) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        tmp = self.path.with_suffix(".json.tmp")
        with self._lock:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)
            try:
                self._mtime = self.path.stat().st_mtime
            except OSError:
                self._mtime = None
            self._cache = data

    @staticmethod
    def _entry(groups: dict, group_id: int | str, create: bool = False) -> dict | None:
        gid = str(group_id)
        item = groups.get(gid)
        if not isinstance(item, dict):
            if not create:
                return None
            item = {"name": "", "updated_at": "", "features": {}}
            groups[gid] = item
        if not isinstance(item.get("features"), dict):
            item["features"] = {}
        return item

    def list_groups(self) -> list[dict]:
        """[{group_id, name, features, updated_at}]，按群号排序。"""
        data = self._load()
        groups = data.get("groups", {})
        out = []
        for gid, item in groups.items():
            if not isinstance(item, dict):
                continue
            features = item.get("features")
            out.append(
                {
                    "group_id": str(gid),
                    "name": str(item.get("name") or ""),
                    "features": dict(features) if isinstance(features, dict) else {},
                    "updated_at": str(item.get("updated_at") or ""),
                }
            )
        out.sort(key=lambda g: (len(g["group_id"]), g["group_id"]))
        return out

    def get_group(self, group_id: int | str) -> dict | None:
        data = self._load()
        item = self._entry(data.get("groups", {}), group_id)
        if item is None:
            return None
        features = item.get("features")
        return {
            "group_id": str(group_id),
            "name": str(item.get("name") or ""),
            "features": dict(features) if isinstance(features, dict) else {},
            "updated_at": str(item.get("updated_at") or ""),
        }

    def override(self, group_id: int | str, feature: str) -> bool | None:
        """该群某功能的覆盖值；None = 跟随全局。"""
        if feature not in FEATURE_KEYS:
            return None
        data = self._load()
        item = self._entry(data.get("groups", {}), group_id)
        if item is None:
            return None
        value = item["features"].get(feature)
        return value if isinstance(value, bool) else None

    # -- 写 --

    def upsert_group(self, group_id: int | str, name: str | None = None) -> bool:
        """补录群；返回是否为新群。已存在且群名无变化时不写盘。"""
        data = self._load()
        groups = data.get("groups", {})
        new_name = (name or "").strip()
        existing = self._entry(groups, group_id)
        if existing is not None and (not new_name or new_name == existing.get("name")):
            return False
        item = self._entry(groups, group_id, create=True)
        is_new = existing is None
        if new_name:
            item["name"] = new_name
        item["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._save(data)
        return is_new

    def set_feature(self, group_id: int | str, feature: str, value: bool | None) -> bool:
        """写入覆盖；value=None 表示恢复跟随全局。返回功能 key 是否有效。"""
        if feature not in FEATURE_KEYS:
            return False
        data = self._load()
        groups = data.get("groups", {})
        item = self._entry(groups, group_id, create=True)
        if value is None:
            item["features"].pop(feature, None)
        else:
            item["features"][feature] = bool(value)
        item["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._save(data)
        return True


_store: GroupFeatureStore | None = None
_store_path: str | None = None


def get_group_feature_store(path: str | Path | None = None) -> GroupFeatureStore:
    global _store, _store_path
    if path is None:
        # 无参调用不重置已存在的实例（允许测试/embedder 重定向后仍生效）
        if _store is None:
            _store = GroupFeatureStore(default_group_features_path())
            _store_path = str(_store.path)
        return _store
    resolved = str(path)
    if _store is None or _store_path != resolved:
        _store = GroupFeatureStore(resolved)
        _store_path = resolved
    return _store


def _global_gate(config: "PluginConfig", group_id: int | str | None, key: str) -> bool:
    if key == "checkin":
        # 签到 = 总开关 + .env 群名单
        return group_id is not None and config.is_checkin_group(group_id)
    return bool(getattr(config, f"{key}_enable", True))


def is_feature_enabled(
    config: "PluginConfig", group_id: int | str | None, key: str
) -> bool:
    """全局总开关 AND 按群覆盖。私聊（group_id=None）只看全局。"""
    if key not in FEATURE_KEYS:
        return True
    if not _global_gate(config, group_id, key):
        return False
    if group_id is None:
        return True
    return get_group_feature_store().override(group_id, key) is not False


# -- 群注册表维护 --

_name_fetch_ts: dict[str, float] = {}


def observe_group(group_id: int | str, name: str | None = None) -> None:
    """群消息到达时同步补录（无 IO API 调用，名字缺失则留空待拉取）。"""
    try:
        get_group_feature_store().upsert_group(group_id, name)
    except Exception:  # noqa: BLE001
        logger.warning("dl_senpai group_features: observe_group failed")


async def refresh_group_name(bot, group_id: int | str) -> None:
    """群名缺失或过期时经 get_group_info 补一次（每群每日最多一次）。"""
    gid = str(group_id)
    now = time.monotonic()
    last = _name_fetch_ts.get(gid, 0.0)
    if now - last < _NAME_REFRESH_SEC:
        return
    _name_fetch_ts[gid] = now
    try:
        info = await bot.call_api("get_group_info", group_id=int(gid))
    except Exception:  # noqa: BLE001
        return
    name = ""
    if isinstance(info, dict):
        name = str(info.get("group_name") or "").strip()
    if name:
        try:
            get_group_feature_store().upsert_group(gid, name)
        except Exception:  # noqa: BLE001
            logger.warning("dl_senpai group_features: refresh name failed")


async def register_groups(bot) -> None:
    """bot 上线时用 get_group_list 种子化注册表，并拉取缺失群名。"""
    try:
        groups = await bot.call_api("get_group_list")
    except Exception:  # noqa: BLE001
        logger.warning("dl_senpai group_features: get_group_list failed")
        return
    store = get_group_feature_store()
    gids: list[str] = []
    for g in groups or []:
        if not isinstance(g, dict):
            continue
        gid = str(g.get("group_id") or "").strip()
        if not gid:
            continue
        gids.append(gid)
        name = str(g.get("group_name") or "").strip()
        store.upsert_group(gid, name or None)
    logger.info(f"[dl_senpai] group_features: registry synced, {len(gids)} groups")
    for gid in gids:
        entry = store.get_group(gid)
        if entry is None or not entry["name"]:
            await refresh_group_name(bot, gid)
