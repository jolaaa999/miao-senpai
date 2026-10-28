"""抖音自主刷：刷推荐流 → 挑一个 → 看描述和热评 → 写日记 / 进记忆本。

跟 bili_browse 的关系：结构照搬，差别只在"能读到什么"。
抖音没有字幕接口，拿到的是描述 + 热评，所以她在抖音写出来的东西天生比 B 站浅——
这是平台给的极限，不是我们偷懒。

为什么点赞/收藏先不做：抖音的赞要么点 DOM（一改版就崩，且点错位置会误触别的按钮），
要么调带 a_bogus 签名的写接口（等于把上面那段解释重写一遍）。读层先跑稳再说。
"""

from __future__ import annotations

import json
import random
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from . import douyin as dy
from .ask_json import ask_json, first_json

__all__ = ["browse_once", "browse_loop"]

_PICK_INSTRUCTION = """你在替一个 QQ 群里的学姐（真人感、爱打游戏、嘴不太甜）刷抖音。

下面是刚刷出来的推荐列表。挑一条你真想点开看的。

规则：
- 挑你感兴趣的，别挑"看起来最正经"的
- 广告、带货、纯标题党跳过
- 一个都不想看就 pick 给 0——不感兴趣就不看，别硬刷

只输出一行 JSON：
{"pick": <序号，从 1 开始，0 = 都不想看>, "why": "<十个字以内，为什么挑它>"}"""

_NOTE_INSTRUCTION = """你刷完了一条抖音。抖音没有字幕，你能拿到的是文字描述、几条热评，
以及几张抽帧画面（抽帧不是完整视频，听不到声音，中间的动作看不到）。

有没有想留下点什么？像人随手在日记里写一句那种。

规则：
- 有事说事，一句话，30 字以内，第一人称
- 画面里看得到的东西可以说，看不见的（声音、中间过程、别人心里想什么）别编
- 热评是别人的话，不是视频内容，别当成事实
- 没什么好说的就 keep 给 false，不要硬写

另外判断：这里面有没有值得**长期记住**的东西（一个知识点、一个之后可能用得上的
信息）。只是"刷到了"就不值得长期记。

只输出一行 JSON：
{"keep": true/false, "note": "<写进日记的那句话>",
 "remember": true/false, "topic": "<三五个字，比如 剪辑/养鱼/买车>"}"""


def _log_path(cfg: Any) -> Path:
    raw = str(getattr(cfg, "douyin_browse_log_file", "") or "")
    return Path(raw or "data/dl_senpai/douyin_browse_log.jsonl")


def _append_log(cfg: Any, record: dict[str, Any]) -> None:
    try:
        path = _log_path(cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _schedule_path(cfg: Any) -> Path:
    raw = str(getattr(cfg, "douyin_browse_schedule_file", "") or "")
    return Path(raw or "data/dl_senpai/douyin_browse_schedule.json")


def _today_count(cfg: Any) -> int:
    """今天刷了几次。跨天自动归零。"""
    path = _schedule_path(cfg)
    today = datetime.now().date().isoformat()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(data, dict) or data.get("day") != today:
        return 0
    return int(data.get("count") or 0)


def _bump_today(cfg: Any) -> None:
    path = _schedule_path(cfg)
    today = datetime.now().date().isoformat()
    count = _today_count(cfg) + 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"day": today, "count": count}), encoding="utf-8")
    except OSError:
        pass


def _memory_view(cfg: Any) -> Any:
    """把 douyin 的记忆文件伪装成 bili_memory 认识的样子。

    为什么不复制一份记忆实现：去重、相似度、上限淘汰这些逻辑跟平台无关，
    抄一遍只会让两边慢慢长歪。bili_memory 只读 file/max 两个字段，套个壳就行。
    """
    return SimpleNamespace(
        bili_memory_file=str(getattr(cfg, "douyin_memory_file", "") or "data/dl_senpai/douyin_memory.json"),
        bili_memory_max=int(getattr(cfg, "douyin_memory_max", 200) or 200),
    )


async def browse_once(cfg: Any, *, llm: Any = None, force: bool = False) -> dict[str, Any]:
    """刷一次抖音。任何一步失败都只记原因，不抛出去。"""
    from .bili_memory import remember
    from .heybox_browse import _in_quiet_hours

    if not bool(getattr(cfg, "douyin_enable", False)) and not force:
        return {"skipped": "douyin_disabled"}
    if _in_quiet_hours(cfg) and not force:
        return {"skipped": "quiet_hours"}
    daily_max = int(getattr(cfg, "douyin_daily_limit", 12) or 0)
    if daily_max > 0 and _today_count(cfg) >= daily_max and not force:
        return {"skipped": "daily_cap"}

    record: dict[str, Any] = {"ts": time.time(), "at": datetime.now().isoformat(timespec="seconds")}
    try:
        videos = await dy.dy_feed(cfg=cfg, limit=12)
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"feed:{type(exc).__name__}:{exc}"[:200]
        _append_log(cfg, record)
        return record
    if not videos:
        record["skipped"] = "empty_feed"
        _append_log(cfg, record)
        return record
    record["feed"] = len(videos)

    listing = "\n".join(
        f"{i}. {v.desc[:46]}（{v.author or '?'}）" for i, v in enumerate(videos, 1)
    )
    pick = first_json(await ask_json(cfg, _PICK_INSTRUCTION, listing, llm=llm))
    index = int(pick.get("pick") or 0)
    record["pick"] = index
    record["why"] = str(pick.get("why") or "")[:30]
    if index < 1 or index > len(videos):
        record["skipped"] = "no_interest"
        _append_log(cfg, record)
        return record

    chosen = videos[index - 1]
    record["aweme_id"] = chosen.aweme_id
    record["title"] = chosen.desc[:60]
    try:
        detail = await dy.dy_video(chosen.aweme_id, cfg=cfg, comment_limit=5)
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"video:{type(exc).__name__}:{exc}"[:200]
        _append_log(cfg, record)
        return record

    # 抖音没有字幕接口，画面就是她的主要信息来源——所以这一层对抖音更重要
    vision: dict[str, Any] = {}
    if bool(getattr(cfg, "video_vision_enable", False)):
        try:
            from .video_vision import describe_video

            vision = await describe_video(
                str(getattr(chosen, "play_url", "") or ""),
                cfg=cfg,
                duration=float(getattr(chosen, "duration_sec", 0) or 0),
                count=int(getattr(cfg, "video_watch_frames", 8) or 8),
                title=chosen.desc,
                referer="https://www.douyin.com/",
            )
            record["vision_frames"] = vision.get("frames")
            record["vision_error"] = vision.get("error", "")
        except Exception as exc:  # noqa: BLE001
            vision = {}
            record["vision_error"] = f"{type(exc).__name__}: {exc}"[:120]

    hot = "\n".join(f"{u}：{t[:60]}" for u, t, _d in (detail.comments or [])[:5]) or "（没抓到热评）"
    payload = (
        f"描述：{detail.desc or chosen.desc}\n"
        f"作者：{detail.author or chosen.author}\n"
        f"数据：赞 {detail.digg} 评论 {detail.comment_count}\n"
        f"热评：\n{hot}"
    )
    if vision.get("scene"):
        payload += (
            f"\n画面（我抽帧看到的）：{vision.get('scene', '')}"
            + (f"\n画面上的字：{vision.get('text')}" if vision.get("text") else "")
            + (f"\n风格：{vision.get('style')}" if vision.get("style") else "")
        )

    decision = first_json(await ask_json(cfg, _NOTE_INSTRUCTION, payload, llm=llm))
    note = str(decision.get("note") or "").strip()
    record["keep"] = bool(decision.get("keep"))

    probability = float(getattr(cfg, "douyin_browse_diary_probability", 1.0) or 0)
    if random.random() > max(0.0, min(1.0, probability)):
        record["skipped"] = "dice"
        _append_log(cfg, record)
        return record
    if not decision.get("keep") or not note:
        record["skipped"] = "nothing_to_say"
        _append_log(cfg, record)
        return record

    if bool(getattr(cfg, "diary_enable", True)):
        try:
            from .diary import get_diary_store

            get_diary_store(getattr(cfg, "diary_dir", None) or None).add(
                {
                    "text": note[:120],
                    "title": f"刷到「{(detail.desc or chosen.desc)[:24]}」",
                    "tags": ["抖音"],
                }
            )
            record["diary"] = note
        except Exception as exc:  # noqa: BLE001
            record["diary_error"] = f"{type(exc).__name__}: {exc}"[:120]

    if bool(decision.get("remember")):
        memo, why = remember(
            _memory_view(cfg),
            bvid=f"dy:{chosen.aweme_id}",
            title=(detail.desc or chosen.desc)[:80],
            note=note,
            topic=str(decision.get("topic") or ""),
            tags=["抖音"],
        )
        record["memo"] = note if memo else ""
        if not memo:
            record["memo_skipped"] = why

    _bump_today(cfg)
    _append_log(cfg, record)
    return record


def _next_gap_seconds(cfg: Any) -> float:
    """间隔还是按作息算（人只有一个作息），但抖音比 B 站慢，基础值更大。"""
    from .heybox_browse import _hour_weight

    low = float(getattr(cfg, "douyin_browse_interval_min_sec", 1800) or 1800)
    high = float(getattr(cfg, "douyin_browse_interval_max_sec", 5400) or 5400)
    base = random.uniform(min(low, high), max(low, high))
    return base / max(0.05, _hour_weight(datetime.now().hour))


async def browse_loop() -> None:
    """后台循环：按作息去刷抖音。"""
    import asyncio

    from .config import get_config
    from nonebot import logger

    from .heybox_browse import _in_quiet_hours, _seconds_until_quiet_end

    cfg0 = get_config()
    logger.info(
        "dl_senpai douyin_browse loop started "
        f"interval={int(getattr(cfg0, 'douyin_browse_interval_min_sec', 1800))}~"
        f"{int(getattr(cfg0, 'douyin_browse_interval_max_sec', 5400))}s "
        f"daily_max={int(getattr(cfg0, 'douyin_daily_limit', 12) or 0)}"
    )
    while True:
        try:
            cfg = get_config()
            if not bool(getattr(cfg, "douyin_enable", False)):
                await asyncio.sleep(1800)
                continue
            if _in_quiet_hours(cfg):
                wait = _seconds_until_quiet_end(cfg)
                logger.info(f"dl_senpai douyin_browse quiet hours, sleep {int(wait)}s")
                await asyncio.sleep(max(300.0, wait))
                continue
            gap = _next_gap_seconds(cfg)
            await asyncio.sleep(max(120.0, gap))
            record = await browse_once(get_config())
            logger.info(
                "dl_senpai douyin_browse tick "
                f"pick={record.get('pick')} note={'yes' if record.get('diary') else 'no'} "
                f"skipped={record.get('skipped') or '-'}"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 循环不能因为一次失败就停
            logger.warning(f"dl_senpai douyin_browse tick failed: {type(exc).__name__}: {exc}")
            await asyncio.sleep(900)
