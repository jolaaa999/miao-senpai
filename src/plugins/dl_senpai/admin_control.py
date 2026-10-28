"""管理员控制状态：让学姐在本群彻底不出声（喵闭嘴 / 喵醒）。

为什么单独一个存储，而不是复用 presence 的「潜水」：

- 潜水是**她自己的**在场状态（说完话先听一会儿，慢慢走开），由时间与话题驱动；
- 静默是**管理员下的命令**（考试周、群里在吵架、太吵了），必须立刻生效、
  带明确到期时间、并且随时能问「还有多久」。

两者语义相反：一个是「她决定不说」，一个是「人不许她说」。如果把静默做成
改潜水参数，下次谁调 presence 的阈值就会让静默失效——而这种失效没人会
立刻发现，只表现为「让她闭嘴了她还在说话」。

存储位置与 group_features.json 同目录（data/dl_senpai/admin/）：
两者都是「人在管她」的状态，控制台（admin/backend）以后可以读同一处数据。

设计取舍：
- 落盘而不是纯内存：静默通常要跨几小时，进程重启（改配置、更新代码）后
  静默消失 = 管理员以为下过命令、其实早就没了。
- 文件小、读频率高（每条消息都要问一次），所以带 mtime 缓存：
  与 group_features.py 同一套写法，避免每条消息一次 IO。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any

try:
    from nonebot import logger
except ImportError:  # 仅单测环境（无 nonebot driver）
    import logging

    logger = logging.getLogger("dl_senpai")  # type: ignore[assignment]


def default_silence_path(project_root: str | Path | None = None) -> Path:
    """静默状态的默认落盘位置（与 group_features.json 同目录）。"""
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[3]
    return root / "data" / "dl_senpai" / "admin" / "silence.json"


class SilenceStore:
    """群静默表：{群号: {until: 到期时间戳, by: 操作者, set_at: 设置时刻}}。

    只认**绝对到期时刻**，不存「剩余秒数」：后者要求读的时候还得知道
    「什么时候写的」，多一个隐含状态就多一处可能算错的地方。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._cache: dict[str, Any] | None = None
        self._mtime: float | None = None

    # -- 读 --

    def _load(self) -> dict[str, Any]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if self._cache is not None and self._mtime == mtime:
            return self._cache
        raw: dict[str, Any] = {}
        if mtime is not None:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    raw = loaded
            except (OSError, json.JSONDecodeError):
                logger.warning(f"dl_senpai silence: failed to read {self.path}")
        groups = raw.get("groups")
        if not isinstance(groups, dict):
            groups = {}
        raw = {"version": 1, "groups": groups}
        self._cache = raw
        self._mtime = mtime
        return raw

    def _save(self, data: dict[str, Any]) -> None:
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

    # -- 查询 --

    def until(self, group_id: int | str) -> float:
        """到期时间戳；没有静默或已过期都返回 0。"""
        if not str(group_id or "").strip():
            return 0.0
        item = self._load()["groups"].get(str(group_id))
        if not isinstance(item, dict):
            return 0.0
        try:
            return float(item.get("until") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def remaining(self, group_id: int | str, *, now: float | None = None) -> int:
        """剩余秒数（向上取整）；没有静默返回 0。"""
        left = self.until(group_id) - (time.time() if now is None else now)
        return max(0, int(left + 0.999))

    def is_silenced(self, group_id: int | str, *, now: float | None = None) -> bool:
        return self.remaining(group_id, now=now) > 0

    def operator(self, group_id: int | str) -> str:
        """是谁下的静默（日志/排障用）。"""
        item = self._load()["groups"].get(str(group_id))
        if not isinstance(item, dict):
            return ""
        return str(item.get("by") or "")

    # -- 写 --

    def set(self, group_id: int | str, seconds: int, *, by: str = "") -> float:
        """设置静默；返回到期时间戳。

        seconds <= 0 视作解除：这样「喵闭嘴 0」和「喵醒」走同一条路径，
        少一个分支就少一处能写错的地方。
        """
        gid = str(group_id or "").strip()
        if not gid:
            raise ValueError("group_id 不能为空")
        if int(seconds) <= 0:
            self.clear(gid)
            return 0.0
        data = self._load()
        until = time.time() + int(seconds)
        data["groups"][gid] = {
            "until": until,
            "by": str(by or ""),
            "set_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "seconds": int(seconds),
        }
        self._save(data)
        return until

    def clear(self, group_id: int | str) -> bool:
        """解除静默；返回是否本来就有静默。"""
        gid = str(group_id or "").strip()
        if not gid:
            return False
        data = self._load()
        existed = data["groups"].pop(gid, None) is not None
        if existed:
            self._save(data)
        return existed

    def prune(self, *, now: float | None = None) -> int:
        """清掉已过期的条目；返回清理条数（防止文件无限长）。"""
        stamp = time.time() if now is None else now
        data = self._load()
        expired = [
            gid
            for gid, item in data["groups"].items()
            if not isinstance(item, dict) or float(item.get("until") or 0) <= stamp
        ]
        for gid in expired:
            data["groups"].pop(gid, None)
        if expired:
            self._save(data)
        return len(expired)


_store: SilenceStore | None = None
_store_path: str | None = None


def get_silence_store(path: str | Path | None = None) -> SilenceStore:
    """取进程级单例；传路径时重定向（测试用）。"""
    global _store, _store_path
    if path is None:
        if _store is None:
            _store = SilenceStore(default_silence_path())
            _store_path = str(_store.path)
        return _store
    resolved = str(Path(path))
    if _store is None or _store_path != resolved:
        _store = SilenceStore(resolved)
        _store_path = resolved
    return _store


def silence_left_seconds(
    group_id: int | str, *, cfg: Any = None, now: float | None = None
) -> int:
    """快捷查询：本群还剩多少秒静默（0 = 没静默）。

    做成模块级函数而不是让调用方拿 store：runtime 那条每消息路径上只需要
    一个数字，不该被迫认识存储实现。

    cfg 用来取自定义的存储路径（控制台或测试会重定向它）。不传就读默认位置——
    注意「指令写在 A 文件、runtime 读 B 文件」这种错位会让静默看起来完全没生效。
    """
    try:
        path = str(getattr(cfg, "admin_silence_path", "") or "").strip() if cfg is not None else ""
        return get_silence_store(path or None).remaining(group_id, now=now)
    except Exception:  # noqa: BLE001 — 读失败当作没静默，宁可多说一句也别整体哑掉
        return 0


# ---------------------------------------------------------------------------
# 警告与黑名单
#
# 为什么和静默放在同一个模块：三者都是「人在管她」的状态，存储位置、
# 缓存策略、原子写入方式完全一样。分成三个模块的唯一结果是三份复制粘贴的
# _load/_save —— 手一滑就会出现「静默用 mtime 缓存、黑名单每次读盘」这种
# 不一致，而这种不一致只在高并发群里才会表现为卡顿。
# ---------------------------------------------------------------------------


class _GroupUserStore:
    """按「群 → 用户」两层组织的 JSON 状态存储。

    子类只需要定义 items_key（内层容器名）和默认文件路径。
    读写策略与 SilenceStore 一致：mtime 缓存 + 临时文件原子替换。
    """

    items_key = "users"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._cache: dict[str, Any] | None = None
        self._mtime: float | None = None

    def _empty(self) -> dict[str, Any]:
        return {"version": 1, "groups": {}}

    def _load(self) -> dict[str, Any]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if self._cache is not None and self._mtime == mtime:
            return self._cache
        data = self._empty()
        if mtime is not None:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("groups"), dict):
                    data = {"version": 1, "groups": loaded["groups"]}
            except (OSError, json.JSONDecodeError):
                logger.warning(f"dl_senpai admin: failed to read {self.path}")
        self._cache = data
        self._mtime = mtime
        return data

    def _save(self, data: dict[str, Any]) -> None:
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

    def _bucket(self, data: dict[str, Any], group_id: int | str, *, create: bool) -> dict[str, Any]:
        gid = str(group_id or "").strip()
        if not gid:
            return {}
        groups = data["groups"]
        bucket = groups.get(gid)
        if not isinstance(bucket, dict):
            if not create:
                return {}
            bucket = {}
            groups[gid] = bucket
        return bucket

    # -- 通用读 --

    def get(self, group_id: int | str, user_id: int | str) -> dict[str, Any]:
        data = self._load()
        bucket = self._bucket(data, group_id, create=False)
        item = bucket.get(str(user_id or "").strip())
        return dict(item) if isinstance(item, dict) else {}

    def contains(self, group_id: int | str, user_id: int | str) -> bool:
        return bool(self.get(group_id, user_id))

    def list_group(self, group_id: int | str) -> list[tuple[str, dict[str, Any]]]:
        """本群所有条目，按写入顺序（字符串键排序，保证输出稳定）。"""
        data = self._load()
        bucket = self._bucket(data, group_id, create=False)
        return [(uid, dict(item)) for uid, item in sorted(bucket.items()) if isinstance(item, dict)]

    def count(self) -> int:
        data = self._load()
        return sum(len(b) for b in data["groups"].values() if isinstance(b, dict))


class WarnStore(_GroupUserStore):
    """群警告表：{群号: {QQ号: {count, last, reasons}}}。

    为什么要有它：管理员说「警告一次」之后，第二次得能接上第一次。
    纯内存的话重启就归零，等于每次重启都在纵容惯犯。
    """

    def add(self, group_id: int | str, user_id: int | str, *, reason: str = "", by: str = "") -> int:
        """记一次警告，返回累计次数。"""
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id 不能为空")
        data = self._load()
        bucket = self._bucket(data, group_id, create=True)
        item = bucket.get(uid)
        if not isinstance(item, dict):
            item = {"count": 0, "reasons": []}
        item["count"] = int(item.get("count") or 0) + 1
        item["last"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        item["by"] = str(by or "")
        reasons = item.get("reasons")
        if not isinstance(reasons, list):
            reasons = []
        if reason:
            reasons.append(str(reason)[:60])
        # 只留最近 5 条理由：这一栏是给人看的，不是审计日志（审计在 data/logs）
        item["reasons"] = reasons[-5:]
        bucket[uid] = item
        self._save(data)
        return int(item["count"])

    def clear(self, group_id: int | str, user_id: int | str | None = None) -> int:
        """清某个人的警告；user_id 为空则清空本群。返回清掉的条数。"""
        data = self._load()
        bucket = self._bucket(data, group_id, create=False)
        if not bucket:
            return 0
        if user_id is None:
            removed = len(bucket)
            data["groups"].pop(str(group_id).strip(), None)
        else:
            removed = 1 if bucket.pop(str(user_id).strip(), None) is not None else 0
        if removed:
            self._save(data)
        return removed


class BlacklistStore(_GroupUserStore):
    """群级黑名单：{群号: {QQ号: {reason, by, at}}}。

    语义只覆盖「她跟不跟他说话」：不拦管理指令（管理员被拉黑会很荒唐），
    也不拦签到/商店（那是群友的积分资产，不该因为一句气话被冻结）。
    """

    def add(self, group_id: int | str, user_id: int | str, *, reason: str = "", by: str = "") -> bool:
        uid = str(user_id or "").strip()
        if not uid:
            raise ValueError("user_id 不能为空")
        data = self._load()
        bucket = self._bucket(data, group_id, create=True)
        existed = uid in bucket
        bucket[uid] = {
            "reason": str(reason or "")[:60],
            "by": str(by or ""),
            "at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._save(data)
        return not existed

    def remove(self, group_id: int | str, user_id: int | str) -> bool:
        data = self._load()
        bucket = self._bucket(data, group_id, create=False)
        removed = bucket.pop(str(user_id or "").strip(), None) is not None
        if removed:
            self._save(data)
        return removed


def default_warn_path(project_root: str | Path | None = None) -> Path:
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[3]
    return root / "data" / "dl_senpai" / "admin" / "warn.json"


def default_blacklist_path(project_root: str | Path | None = None) -> Path:
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[3]
    return root / "data" / "dl_senpai" / "admin" / "blacklist.json"


_warn_store: WarnStore | None = None
_warn_path: str | None = None
_blacklist_store: BlacklistStore | None = None
_blacklist_path: str | None = None


def _path_of(cfg: Any, attr: str) -> str:
    return str(getattr(cfg, attr, "") or "").strip() if cfg is not None else ""


def get_warn_store(path: str | Path | None = None) -> WarnStore:
    global _warn_store, _warn_path
    if path is None:
        if _warn_store is None:
            _warn_store = WarnStore(default_warn_path())
            _warn_path = str(_warn_store.path)
        return _warn_store
    resolved = str(Path(path))
    if _warn_store is None or _warn_path != resolved:
        _warn_store = WarnStore(resolved)
        _warn_path = resolved
    return _warn_store


def get_blacklist_store(path: str | Path | None = None) -> BlacklistStore:
    global _blacklist_store, _blacklist_path
    if path is None:
        if _blacklist_store is None:
            _blacklist_store = BlacklistStore(default_blacklist_path())
            _blacklist_path = str(_blacklist_store.path)
        return _blacklist_store
    resolved = str(Path(path))
    if _blacklist_store is None or _blacklist_path != resolved:
        _blacklist_store = BlacklistStore(resolved)
        _blacklist_path = resolved
    return _blacklist_store


def is_blacklisted(group_id: int | str, user_id: int | str, *, cfg: Any = None) -> bool:
    """快捷查询：这个人是不是在本群被拉黑了。

    和 silence_left_seconds 一样做成模块级函数：runtime 那条每消息路径上
    只需要一个是/否，不该被迫认识存储实现。读失败一律当作「没拉黑」——
    存储坏了就让人正常说话，比让人莫名其妙消失要好。
    """
    try:
        path = _path_of(cfg, "admin_blacklist_path")
        return get_blacklist_store(path or None).contains(group_id, user_id)
    except Exception:  # noqa: BLE001
        return False


__all__ = [
    "SilenceStore",
    "WarnStore",
    "BlacklistStore",
    "default_silence_path",
    "default_warn_path",
    "default_blacklist_path",
    "get_silence_store",
    "get_warn_store",
    "get_blacklist_store",
    "silence_left_seconds",
    "is_blacklisted",
]

