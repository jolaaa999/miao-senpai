"""周报：每周一次「我们这周」。

为什么要它
----------
她记的东西都是碎片：日记一条、印象一条、画一张。人回头看才知道哪些是
变化——周报就是那个「回头看」。原版把它列在功能清单里（「周报等等…」），
实现上其实很简单：本地数据本来就全在，缺的只是汇总和一次主动发送。

三条纪律
--------
1. **只发白名单群**，跟 own_life 同一条硬规则：主动外发是唯一没人邀请的动作。
2. **一周最多一次**，按「本周已发」记在 state 里，重启不会重发。
3. **数据拿不到就不编**：每一个数据源都是 best-effort，缺了就在周报里少一行，
   绝不让模型凭空补一个「这周聊了 300 句」。
"""

from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path
from typing import Any

from nonebot import logger

from .storage import JsonStore

__all__ = ["collect_stats", "compose_report", "run_weekly", "weekly_loop"]

_WEEK_SEC = 7 * 24 * 3600


def _dir_of(cfg: Any, key: str, default: str) -> Path:
    raw = getattr(cfg, key, None) or default
    return Path(str(raw))


def _count_jsonl(path: Path, since: float) -> int:
    """数一个 jsonl 里「since 之后」的行数。坏行跳过，不抛。"""
    if not path.exists():
        return 0
    count = 0
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            # 不看时间戳字段名：不同模块分别用 at / time / ts，逐个试
            try:
                import json

                data = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(data, dict):
                continue
            stamp = data.get("at") or data.get("time") or data.get("ts") or 0
            try:
                value = float(stamp)
            except (TypeError, ValueError):
                value = 0.0
            if value >= since:
                count += 1
    except OSError:
        return count
    return count


def _count_files(path: Path, since: float) -> int:
    if not path.exists():
        return 0
    count = 0
    try:
        for item in path.rglob("*"):
            if not item.is_file():
                continue
            try:
                if item.stat().st_mtime >= since:
                    count += 1
            except OSError:
                continue
    except OSError:
        return count
    return count


def collect_stats(cfg: Any, *, now: float | None = None) -> dict[str, int]:
    """收集这周的数字。全部 best-effort：拿不到就是 0，不猜。"""
    now = now or time.time()
    since = now - _WEEK_SEC
    stats: dict[str, int] = {}
    try:
        stats["diary"] = _count_jsonl(
            _dir_of(cfg, "diary_dir", "data/dl_senpai/diary") / "index.jsonl", since
        )
    except Exception:  # noqa: BLE001
        stats["diary"] = 0
    try:
        stats["draws"] = _count_jsonl(
            _dir_of(cfg, "draw_dir", "data/dl_senpai/draws") / "index.jsonl", since
        )
    except Exception:  # noqa: BLE001
        stats["draws"] = 0
    try:
        stats["gallery"] = _count_jsonl(
            _dir_of(cfg, "gallery_dir", "data/dl_senpai/gallery") / "index.jsonl", since
        )
    except Exception:  # noqa: BLE001
        stats["gallery"] = 0
    try:
        stats["chat_files"] = _count_files(
            _dir_of(cfg, "memory_dir", "data/dl_senpai/memory"), since
        )
    except Exception:  # noqa: BLE001
        stats["chat_files"] = 0
    return stats


async def compose_report(cfg: Any, stats: dict[str, int], *, llm: Any = None) -> str:
    """让模型把这堆数字写成一段人话。模型不可用时退化成纯数据。"""
    if llm is None:
        try:
            from .llm import get_llm

            llm = get_llm(cfg)
        except Exception:  # noqa: BLE001
            llm = None

    facts = "\n".join(f"- {k}: {v}" for k, v in stats.items() if v)
    if not facts:
        return ""

    prompt = (
        "下面是这一周你自己留下的痕迹（本地统计，不是别人给你的总结）：\n"
        f"{facts}\n\n"
        "用你自己的口气写一小段「这周」：三到五句，像翻日记时随口说的话，"
        "不要列清单，不要写「本周总结」这种标题，不要祝贺任何人。"
        "数字只挑最有意思的一两个提，其它不提。"
    )
    if llm is None:
        return "这周：" + "，".join(f"{k} {v}" for k, v in stats.items() if v)

    try:
        text = await llm.chat([{"role": "user", "content": prompt}], max_tokens=400)
        return str(text or "").strip()
    except Exception as exc:  # noqa: BLE001 — 模型挂了就退化成数据行
        logger.warning(f"dl_senpai weekly compose failed: {type(exc).__name__}: {exc}")
        return "这周：" + "，".join(f"{k} {v}" for k, v in stats.items() if v)


def _state_path(cfg: Any) -> Path:
    base = _dir_of(cfg, "memory_dir", "data/dl_senpai/memory").parent
    return base / "weekly" / "state.json"


def _week_key(now: datetime) -> str:
    iso = now.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


async def run_weekly(bot: Any, cfg: Any, *, now: datetime | None = None) -> str:
    """到点就跑一次。返回结果标签，便于日志与单测。"""
    if not bool(getattr(cfg, "weekly_enable", False)):
        return "disabled"
    now = now or datetime.now()
    if now.weekday() + 1 != int(getattr(cfg, "weekly_weekday", 1) or 1):
        return "not_today"
    if now.hour != int(getattr(cfg, "weekly_hour", 22) or 22):
        return "not_now"

    deadline = _week_key(now)
    state_path = _state_path(cfg)
    state = JsonStore(state_path, create_parent=True).load(default={})
    if str(state.get("last_week") or "") == deadline:
        return "already_sent"

    groups = list(getattr(cfg, "weekly_group_ids", lambda: [])())
    stats = collect_stats(cfg)
    text = await compose_report(cfg, stats)
    if not text:
        JsonStore(state_path, create_parent=True).save({"last_week": deadline, "empty": True})
        return "empty"

    if not groups:
        # 没配群就只落盘、不发。跟 own_life 一致：主动外发必须有白名单。
        JsonStore(state_path, create_parent=True).save(
            {"last_week": deadline, "text": text, "sent": False}
        )
        logger.info("dl_senpai weekly composed but no weekly_groups, saved only")
        return "saved_no_group"

    if bool(getattr(cfg, "dry_run", False)):
        logger.info(f"[干跑] weekly 会在群 {groups} 发（{len(text)} 字）：{text}")
        JsonStore(state_path, create_parent=True).save(
            {"last_week": deadline, "text": text, "sent": False, "dry_run": True}
        )
        return "dry_run"

    from .send_guard import get_send_guard

    guard = get_send_guard()
    allowed, reason = guard.before_send()
    if not allowed:
        logger.info(f"dl_senpai weekly blocked ({reason})")
        return "blocked"

    group_id = groups[0]
    try:
        await bot.send_group_msg(group_id=int(group_id), message=text)
    except Exception as exc:  # noqa: BLE001
        guard.on_failure()
        logger.warning(f"dl_senpai weekly send failed group={group_id}: {exc}")
        return "failed"

    guard.on_success()
    JsonStore(state_path, create_parent=True).save(
        {"last_week": deadline, "text": text, "sent": True, "at": time.time()}
    )
    logger.info(f"dl_senpai weekly sent group={group_id} chars={len(text)}")
    return "sent"


_loop_started = False


async def weekly_loop(bot: Any) -> None:
    """每 30 分钟看一眼「是不是该发周报了」。

    为什么不是精确 sleep 到点：精确计时在休眠/唤醒的机器上会漂，
    而且改配置（换到周日 21 点）之后要重启才生效。半小时一查，
    代价是一件事，收益是「改完立刻生效」。
    """
    global _loop_started
    if _loop_started:
        return
    _loop_started = True
    import asyncio

    while True:
        try:
            from .config import get_config

            cfg = get_config()
            if bool(getattr(cfg, "weekly_enable", False)):
                await run_weekly(bot, cfg)
        except Exception as exc:  # noqa: BLE001 — 循环不能死
            logger.warning(f"dl_senpai weekly loop error: {type(exc).__name__}: {exc}")
        await asyncio.sleep(1800)
