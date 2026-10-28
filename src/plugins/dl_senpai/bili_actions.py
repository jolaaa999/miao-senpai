"""点赞/收藏的当日计数。

为什么单独一个账本：这两个动作没有文案、没有"发过没发过"的问题，
只需要回答两件事——今天点了几次、这条是不是已经点过。write_guard 那套
（文案去重、间隔、同帖一次）对点赞完全不适用，硬套只会读起来别扭。

重置按自然日做：跨天第一次读就整本换新，不需要定时任务。
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any

__all__ = ["already", "can", "count_today", "note"]

_DEFAULT = "data/dl_senpai/bili_actions.json"


def _path(cfg: Any) -> Path:
    raw = str(getattr(cfg, "bili_actions_file", "") or "")
    return Path(raw or _DEFAULT)


def _load(cfg: Any) -> dict[str, Any]:
    path = _path(cfg)
    today = date.today().isoformat()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if not isinstance(data, dict) or data.get("day") != today:
        # 跨天就整本清零：昨天下没下过赞跟今天无关
        return {"day": today}
    return data


def _save(cfg: Any, data: dict[str, Any]) -> None:
    path = _path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        # 记账失败最多是今天多赞几个，不该挡刷帖
        pass


def _rows(cfg: Any, kind: str) -> list[dict[str, Any]]:
    data = _load(cfg)
    rows = data.get(kind)
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def count_today(cfg: Any, kind: str) -> int:
    return len(_rows(cfg, kind))


def already(cfg: Any, kind: str, bvid: str) -> bool:
    return any(str(r.get("bvid") or "") == bvid for r in _rows(cfg, kind))


def can(cfg: Any, kind: str, daily_max: int) -> bool:
    """daily_max<=0 当作不限。"""
    if daily_max <= 0:
        return True
    return count_today(cfg, kind) < daily_max


def note(cfg: Any, kind: str, bvid: str, *, title: str = "") -> None:
    data = _load(cfg)
    rows = data.get(kind)
    rows = [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []
    rows.append({"bvid": bvid, "title": title[:60], "ts": time.time()})
    data[kind] = rows
    _save(cfg, data)
