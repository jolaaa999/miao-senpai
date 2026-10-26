"""按天计数的动作配额（B站 / 小黑盒这类会留痕的动作共用）。

为什么不复用 ToolDefinition.rate_limit：那个是「N 秒内只放一次」的节流，
管不住「今天总共只许 40 次」。社区里有人给自家 AI 开「每天 4 小时观看
配额」就是同一个思路——怕她半夜连着刷一百个视频、或者被群里人当搜索
接口白嫖。

存储：一个 JSON 文件，形如
    {"date": "2026-10-25", "counters": {"bili": 3, "heybox": 1}}
跨天自动清零。读写都在同一把锁里，避免同一轮里多个工具并发结算时
出现「读-改-写」丢计数（那会让配额形同虚设）。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["DEFAULT_PATH", "check_quota", "quota_path", "read_usage", "reset_quota"]

DEFAULT_PATH = "data/dl_senpai/quota.json"

# 一把进程级锁：配额是全局资源，多协程同时扣必须串行
_LOCK = asyncio.Lock()


def quota_path(cfg: Any = None) -> Path:
    """配额文件路径。配置里没有单独开关，跟着 data 目录走。"""
    raw = ""
    if cfg is not None:
        raw = str(getattr(cfg, "quota_file", "") or "")
    return Path(raw or DEFAULT_PATH)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"date": _today(), "counters": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"date": _today(), "counters": {}}
    if not isinstance(raw, dict):
        return {"date": _today(), "counters": {}}
    if str(raw.get("date") or "") != _today():
        # 换天了：直接重开一份，不迁移旧计数
        return {"date": _today(), "counters": {}}
    counters = raw.get("counters")
    if not isinstance(counters, dict):
        counters = {}
    return {"date": _today(), "counters": counters}


def _save(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


async def read_usage(name: str, cfg: Any = None) -> int:
    """查今天某个计数器用了几次（不占配额）。"""
    path = quota_path(cfg)
    async with _LOCK:
        return int(_load(path)["counters"].get(name) or 0)


async def check_quota(name: str, limit: int, cfg: Any = None) -> tuple[bool, int]:
    """占一格配额。

    返回 (是否放行, 放行后今天的累计次数)。
    设计成「先扣再用」：先加一再看有没有超，避免两个并发请求同时看到
    「还没满」然后一起放出去。用例失败时调用方可以调 refund_quota 退回。
    """
    if limit <= 0:
        # limit<=0 视为不限（内部调用、或用户在配置里写成 0）
        return True, 0
    path = quota_path(cfg)
    async with _LOCK:
        data = _load(path)
        used = int(data["counters"].get(name) or 0)
        if used >= limit:
            return False, used
        data["counters"][name] = used + 1
        _save(path, data)
        return True, used + 1


async def refund_quota(name: str, cfg: Any = None) -> None:
    """把刚占的那一格退回去（网络超时、接口报错这类「没真做事」的情况）。"""
    path = quota_path(cfg)
    async with _LOCK:
        data = _load(path)
        used = int(data["counters"].get(name) or 0)
        if used > 0:
            data["counters"][name] = used - 1
            _save(path, data)


async def reset_quota(cfg: Any = None) -> None:
    """清空今天的计数（测试与手动救急用）。"""
    path = quota_path(cfg)
    async with _LOCK:
        _save(path, {"date": _today(), "counters": {}})
