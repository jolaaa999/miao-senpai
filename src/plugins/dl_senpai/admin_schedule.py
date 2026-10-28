"""群定时消息：管理员让学姐在某个时刻替群里发一句话（喵/定时）。

为什么需要落盘 + 后台循环，而不是 asyncio.call_later：

- call_later 活在进程里，改配置重启一次全丢——而「明早八点提醒大家交作业」
  正好会跨过夜里的自动更新；
- 落盘之后可以随时「喵/定时」查还剩哪些，管理员看得见才敢用。

为什么不做成通用 cron：这条指令的使用场景是「替群里发一句人话」，
不是跑运维脚本。支持到「N 分钟/小时后发一次」已经覆盖 95% 的用法，
再加表达式语法只会让「写错了但看起来像对的」变得更常见。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from threading import Lock
from typing import Any

try:
    from nonebot import logger
except ImportError:  # 仅单测环境
    import logging

    logger = logging.getLogger("dl_senpai")  # type: ignore[assignment]

#: 一次最多排多少条（防止某人刷一万条把循环拖死）。
MAX_PER_GROUP = 50
#: 最长提前多久：超过这个数的任务几乎一定是打错了（比如把 30m 打成 30w）。
MAX_DELAY_SECONDS = 30 * 86400
#: 扫描间隔。20 秒对「提醒类」任务足够准，又不至于每秒读盘。
TICK_SECONDS = 20.0

_ADD_RE = None  # 延迟到 parse 时编译，避免模块导入期依赖 re 的顺序问题


def default_schedule_path(project_root: str | Path | None = None) -> Path:
    root = Path(project_root) if project_root else Path(__file__).resolve().parents[3]
    return root / "data" / "dl_senpai" / "admin" / "schedule.json"


class TimerStore:
    """群定时表：{群号: [{id, at, text, by}]}，at 是绝对到期时间戳。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._cache: dict[str, Any] | None = None
        self._mtime: float | None = None

    def _load(self) -> dict[str, Any]:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            mtime = None
        if self._cache is not None and self._mtime == mtime:
            return self._cache
        data: dict[str, Any] = {"version": 1, "groups": {}}
        if mtime is not None:
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("groups"), dict):
                    data = {"version": 1, "groups": loaded["groups"]}
            except (OSError, json.JSONDecodeError):
                logger.warning(f"dl_senpai schedule: failed to read {self.path}")
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

    def _items(self, data: dict[str, Any], group_id: int | str) -> list[dict[str, Any]]:
        bucket = data["groups"].get(str(group_id or "").strip())
        return [item for item in bucket if isinstance(item, dict)] if isinstance(bucket, list) else []

    def add(
        self, group_id: int | str, delay_seconds: int, text: str, *, by: str = ""
    ) -> tuple[int, str]:
        """排一条；返回 (id, 失败原因)。"""
        body = (text or "").strip()
        if not body:
            return 0, "要发什么内容呢？"
        if delay_seconds <= 0:
            return 0, "时间要到「以后」才行。"
        if delay_seconds > MAX_DELAY_SECONDS:
            return 0, "太远了，最多排 30 天内的。"
        gid = str(group_id or "").strip()
        data = self._load()
        items = data["groups"].setdefault(gid, [])
        if len(items) >= MAX_PER_GROUP:
            return 0, f"这个群已经排了 {len(items)} 条（上限 {MAX_PER_GROUP}）。"
        task_id = int(time.time() * 1000) % 1000000
        while any(int(item.get("id") or 0) == task_id for item in items):
            task_id += 1
        items.append(
            {
                "id": task_id,
                "at": time.time() + int(delay_seconds),
                "text": body,
                "by": str(by or ""),
            }
        )
        self._save(data)
        return task_id, ""

    def list_group(self, group_id: int | str) -> list[dict[str, Any]]:
        items = self._items(self._load(), group_id)
        return sorted(items, key=lambda item: float(item.get("at") or 0))

    def remove(self, group_id: int | str, task_id: int) -> bool:
        gid = str(group_id or "").strip()
        data = self._load()
        items = data["groups"].get(gid)
        if not isinstance(items, list):
            return False
        kept = [item for item in items if int(item.get("id") or 0) != int(task_id)]
        if len(kept) == len(items):
            return False
        if kept:
            data["groups"][gid] = kept
        else:
            data["groups"].pop(gid, None)
        self._save(data)
        return True

    def due(self, *, now: float | None = None) -> list[tuple[str, dict[str, Any]]]:
        """取所有到期任务，并**顺手从表里删掉**（避免重复发送）。"""
        stamp = time.time() if now is None else now
        data = self._load()
        fired: list[tuple[str, dict[str, Any]]] = []
        changed = False
        for gid, items in list(data["groups"].items()):
            if not isinstance(items, list):
                continue
            keep: list[dict[str, Any]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                if float(item.get("at") or 0) <= stamp:
                    fired.append((gid, item))
                    changed = True
                else:
                    keep.append(item)
            if keep:
                data["groups"][gid] = keep
            elif changed:
                data["groups"].pop(gid, None)
        if changed:
            self._save(data)
        return fired


_store: TimerStore | None = None
_store_path: str | None = None


def get_timer_store(path: str | Path | None = None) -> TimerStore:
    global _store, _store_path
    if path is None:
        if _store is None:
            _store = TimerStore(default_schedule_path())
            _store_path = str(_store.path)
        return _store
    resolved = str(Path(path))
    if _store is None or _store_path != resolved:
        _store = TimerStore(resolved)
        _store_path = resolved
    return _store


def _cfg_path(cfg: Any) -> str:
    return str(getattr(cfg, "admin_schedule_path", "") or "").strip() if cfg is not None else ""


def _cfg_of_ctx(ctx: Any) -> Any:
    cfg = getattr(ctx, "config", None)
    if cfg is not None:
        return cfg
    try:
        from .config_runtime.runtime import get_config

        return get_config()
    except Exception:  # noqa: BLE001
        return None


async def _send_group(bot: Any, group_id: str, text: str) -> tuple[bool, str]:
    """发一条群消息：两种暴露方式都试，失败原因带回去。"""
    fn = getattr(bot, "send_group_msg", None)
    try:
        if fn is not None:
            result = fn(group_id=int(group_id), message=text)
            if hasattr(result, "__await__"):
                await result
            return True, ""
        await bot.call_api("send_group_msg", group_id=int(group_id), message=text)
        return True, ""
    except Exception as exc:  # noqa: BLE001 — 定时发送失败要写日志，不能让循环挂掉
        return False, f"{type(exc).__name__}: {exc}"


async def fire_due(bot: Any, *, store: TimerStore | None = None, now: float | None = None) -> int:
    """把到期的任务发出去；返回成功条数。

    单独抽出来是为了能在测试里直接调，不用等 20 秒的循环 tick。
    """
    target = store or get_timer_store()
    fired = target.due(now=now)
    sent = 0
    for gid, item in fired:
        text = str(item.get("text") or "")
        ok, err = await _send_group(bot, gid, text)
        if ok:
            sent += 1
            logger.info(f"dl_senpai schedule: sent to {gid}")
        else:
            # 发失败也不重新排队：重试可能造成重复消息，而「没发出去」
            # 至少是确定的失败，比在群里刷三遍同一句话好。
            logger.warning(f"dl_senpai schedule: send failed group={gid} err={err}")
    return sent


async def timer_loop(bot: Any, interval: float = TICK_SECONDS) -> None:
    """后台循环：每 interval 秒检查一次到期任务。"""
    while True:
        try:
            await asyncio.sleep(interval)
            await fire_due(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 循环绝不能因为一次异常就退出
            logger.warning(f"dl_senpai schedule loop: {type(exc).__name__}: {exc}")


def parse_timer_command(raw: str) -> tuple[str, int, str]:
    """解析「喵/定时」的参数：返回 (子命令, 秒数, 内容)。

        加 30m 记得交作业  → (add, 1800, 记得交作业)
        列表               → (list, 0, )
        删 123456          → (del, 123456, )
        清空               → (clear, 0, )
    """
    text = (raw or "").strip()
    if not text or text in ("列表", "list"):
        return "list", 0, ""
    if text in ("清空", "clear"):
        return "clear", 0, ""
    if text.startswith(("删", "删除", "del")):
        rest = text.lstrip("删del").strip()
        rest = rest.lstrip("除").strip()
        try:
            return "del", int(rest), ""
        except (TypeError, ValueError):
            return "del", 0, ""
    body = text
    for prefix in ("加", "添加", "新增", "add"):
        if body.startswith(prefix):
            body = body[len(prefix) :].strip()
            break
    head, _, tail = body.partition(" ")
    if not tail:
        # 也允许「30m记得交作业」这种没空格的写法
        import re

        matched = re.match(r"^(\d{1,6}[a-zA-Z秒分时天周小]*)(.+)$", body)
        if matched:
            head, tail = matched.group(1), matched.group(2)
    from .admin_commands import parse_duration

    seconds = parse_duration(head, default_unit_seconds=60)
    if seconds is None:
        return "add", 0, ""
    return "add", int(seconds), tail.strip()


async def handle_timer_command(ctx: Any, args: Any, raw: str) -> None:
    """喵/定时：排一条、看列表、删一条、清空。"""
    from .admin_commands import _fmt_duration, _int_gid, _reply

    gid = _int_gid(ctx)
    if gid is None:
        await _reply(ctx, "定时消息是发到群里的，只能在群里用呢。")
        return
    store = get_timer_store(_cfg_path(_cfg_of_ctx(ctx)) or None)
    action, value, text = parse_timer_command(raw)

    if action == "add":
        if value <= 0 or not text:
            await _reply(ctx, "用法：喵/定时 加 30m 记得交作业（30m = 半小时后）")
            return
        task_id, why = store.add(gid, value, text, by=str(getattr(ctx, "user_id", "") or ""))
        if why:
            await _reply(ctx, f"没排上：{why}")
            return
        await _reply(ctx, f"排好了：{_fmt_duration(value)}后我发「{text}」（编号 {task_id}）。")
        return
    if action == "del":
        if not value:
            await _reply(ctx, "要删哪一条？先「喵/定时」看编号。")
            return
        removed = store.remove(gid, value)
        await _reply(ctx, f"删掉了 {value}。" if removed else f"没有编号 {value} 的定时。")
        return
    if action == "clear":
        items = store.list_group(gid)
        for item in items:
            store.remove(gid, int(item.get("id") or 0))
        await _reply(ctx, f"清空了本群定时（{len(items)} 条）。")
        return

    items = store.list_group(gid)
    if not items:
        await _reply(ctx, "本群没有排着的定时消息。")
        return
    now = time.time()
    lines = [f"本群有 {len(items)} 条定时："]
    for item in items[:30]:
        left = max(0, int(float(item.get("at") or 0) - now))
        when = time.strftime("%m-%d %H:%M", time.localtime(float(item.get("at") or 0)))
        lines.append(f"  {item.get('id')} · {when}（还有 {_fmt_duration(left)}）· {str(item.get('text') or '')[:30]}")
    await _reply(ctx, "\n".join(lines))


__all__ = [
    "TimerStore",
    "default_schedule_path",
    "get_timer_store",
    "parse_timer_command",
    "handle_timer_command",
    "fire_due",
    "timer_loop",
    "TICK_SECONDS",
]
