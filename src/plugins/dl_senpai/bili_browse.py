"""B站自主刷：她自己点开几个视频看看，觉得有意思就记进日记。

跟小黑盒的区别
--------------
小黑盒那套是「逛 -> 读 -> 可能说一句话」；B站这边**只读**：
不点赞、不投币、不评论、不发弹幕。原因有两个：

1. 点赞/评论是公开互动，而她的号才 Lv1（B站要 Lv2 才能评论），
   现在能做的动作只有"看"，硬凑互动只会变成刷量特征；
2. 浏览是最安全的自主活动：不加任何一条会被别人看见的记录。

那她"看了"算什么产出？写日记。她的日记本来就是"我自己的一天"，
"今天在B站刷到一个讲XX的"完全属于这个范畴，也不需要动任何平台接口。

节奏
----
比小黑盒稀（默认 90~240 分钟、每天最多 8 次）。原因很实在：B站首页
推荐刷太快，看的东西就都一样了，反而不像人在刷。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["browse_once", "bili_browse_loop", "recent_runs"]

_PICK_INSTRUCTION = """你在 B 站首页刷到下面这些视频，只能点开一个。

挑一个你现在最想看的。都不想看就 pick 给 0（真的可以不看）。

只输出一行 JSON：
{"pick": <序号>, "why": "<不超过 15 字，为什么点这个>"}"""

_NOTE_INSTRUCTION = """你看了一个 B 站视频。你能拿到标题、简介、热评、字幕稿，
如果下面还给了「我抽帧看到的」，那是真的从画面里看到的东西。
抽帧不是完整视频，听不到声音，中间的动作看不到。

看完有没有想留下点什么？像人随手在日记里写一句那种。

规则：
- 有事说事，一句话，30 字以内，第一人称
- 有字幕就说视频里真讲到的东西；没有字幕就只凭标题和简介说，别编
- 字幕是机器听的，可能有错字，所以别逐字引用、别报数字型号，说大意
- 有抽帧画面就可以说画面里有什么，但别夸"剪得好/节奏真棒"——剪辑节奏抽帧看不出来
- 没什么好说的就 keep 给 false，不要硬写

另外判断：这里面有没有值得**长期记住**的东西（一个知识点、一个之后可能用得上的
信息、一个你真的想弄明白的事）。只是"看过了"就不值得长期记。

另外判断要不要**收藏**（收藏是"以后还想回来看"的意思，不是"挺好的"）：
- 只有真会回看才收：教程、方法、以后要照做的、想反复看的
- 只是有趣、只是看完爽了，收藏给 false

只输出一行 JSON：
{"keep": true/false, "note": "<写进日记的那句话>",
 "remember": true/false, "topic": "<三五个字，比如 买车/剪辑/显卡>",
 "fav": true/false, "fav_why": "<为什么值得回看，几个字>"}"""


_COMMENT_INSTRUCTION = """你刚点开一个 B 站视频，看完了。你没看画面，但如果有字幕，就等于你听过内容。

在评论区说一句。短，一句或两句，像真人随手打字。

- 有字幕就可以说内容本身：里面讲的方法、参数、观点里你最有反应的那一点
- 不要总结视频，不要夸"剪得好/拍得真棒"——你没看过画面
- 字幕是机器听的，可能有错字，别逐字引用
- 不要"感谢分享"，不要写小作文，不带链接和联系方式

只输出一行 JSON：
{"text": "<一句评论>"}"""


async def _react(cfg: Any, video: Any, decision: dict[str, Any], record: dict[str, Any]) -> None:
    """点赞（刷到就赞）+ 收藏（模型说值得回看才收）。失败只记账，不抛。"""
    from . import bili as bl
    from . import bili_actions as ba

    bvid = str(getattr(video, "bvid", "") or "")
    title = str(getattr(video, "title", "") or "")
    if not bvid:
        return

    like_mode = str(getattr(cfg, "bili_like_mode", "always") or "always").lower()
    want_like = bool(
        getattr(cfg, "bili_like_enable", True)
        and (like_mode == "always" or bool(decision.get("like")))
    )
    if want_like:
        if ba.already(cfg, "like", bvid):
            record["like"] = "already"
        elif not ba.can(cfg, "like", int(getattr(cfg, "bili_like_daily_max", 12) or 0)):
            record["like"] = "daily_cap"
        else:
            try:
                await bl.bili_like(bvid, cfg=cfg)
                ba.note(cfg, "like", bvid, title=title)
                record["like"] = True
            except Exception as exc:  # noqa: BLE001
                record["like_error"] = f"{type(exc).__name__}: {exc}"[:140]

    if bool(getattr(cfg, "bili_fav_enable", True)) and bool(decision.get("fav")):
        if ba.already(cfg, "fav", bvid):
            record["fav"] = "already"
        elif not ba.can(cfg, "fav", int(getattr(cfg, "bili_fav_daily_max", 6) or 0)):
            record["fav"] = "daily_cap"
        else:
            try:
                sent = await bl.bili_favorite(bvid, cfg=cfg)
                ba.note(cfg, "fav", bvid, title=title)
                record["fav"] = True
                record["fav_folder"] = sent.get("folder_id")
                record["fav_why"] = str(decision.get("fav_why") or "")[:30]
            except Exception as exc:  # noqa: BLE001
                record["fav_error"] = f"{type(exc).__name__}: {exc}"[:140]


def _log_path(cfg: Any) -> Path:
    raw = str(getattr(cfg, "bili_browse_log_file", "") or "")
    return Path(raw or "data/dl_senpai/bili_browse_log.jsonl")


def _recent_bvids(cfg: Any, *, hours: float = 12) -> set[str]:
    """最近刷过的 bvid。只读台账，不用另存状态。"""
    path = _log_path(cfg)
    if not path.exists():
        return set()
    since = time.time() - max(1.0, hours) * 3600
    out: set[str] = set()
    try:
        with path.open("r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if float(row.get("ts") or 0) < since:
                    continue
                bvid = str(row.get("bvid") or "")
                if bvid:
                    out.add(bvid)
    except OSError:
        return set()
    return out


def _append_log(cfg: Any, record: dict[str, Any]) -> None:
    path = _log_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def recent_runs(cfg: Any, limit: int = 10) -> list[dict[str, Any]]:
    path = _log_path(cfg)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _schedule_path(cfg: Any) -> Path:
    raw = str(getattr(cfg, "bili_browse_schedule_file", "") or "")
    return Path(raw or "data/dl_senpai/bili_browse_schedule.json")


def _count_today(cfg: Any) -> int:
    """今天刷了几次。跟小黑盒分开记：两边的额度不该互相吃掉。"""
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        data = json.loads(_schedule_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    return int(data.get("count") or 0) if str(data.get("date") or "") == today else 0


def _bump_today(cfg: Any) -> int:
    path = _schedule_path(cfg)
    count = _count_today(cfg) + 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"date": datetime.now().strftime("%Y-%m-%d"), "count": count}),
            encoding="utf-8",
        )
    except OSError:
        pass
    return count


def _next_gap_seconds(cfg: Any) -> int:
    """下一次多久之后刷。作息权重跟小黑盒共用一套表（人只有一个作息）。"""
    from .heybox_browse import _hour_weight, _seconds_until_quiet_end

    quiet_wait = _seconds_until_quiet_end(cfg)
    if quiet_wait > 0:
        return int(quiet_wait)

    low = int(getattr(cfg, "bili_browse_interval_min_sec", 5400) or 5400)
    high = int(getattr(cfg, "bili_browse_interval_max_sec", 14400) or 14400)
    low, high = min(low, high), max(low, high)
    weight = max(0.2, _hour_weight(datetime.now().hour))
    gap = random.uniform(low, high) / weight
    return int(max(60, min(gap, 6 * 3600)))


async def browse_once(cfg: Any, *, llm: Any = None, force: bool = False) -> dict[str, Any]:
    """刷一次 B 站：看首页 -> 挑一个视频 -> 看标题/简介/热评 -> 可能记进日记。

    任何一步失败都只记原因，不抛出去："她今天没刷成"不该炸掉机器人。
    """
    from . import bili as bl
    from .ask_json import ask_json, first_json
    from .heybox_browse import _in_quiet_hours

    if not bool(getattr(cfg, "bili_browse_enable", False)) and not force:
        return {"skipped": "bili_browse_disabled"}
    if _in_quiet_hours(cfg) and not force:
        return {"skipped": "quiet_hours"}

    record: dict[str, Any] = {"ts": time.time(), "at": datetime.now().isoformat(timespec="seconds")}

    try:
        # 每轮都"刷新一次首页"：推荐流每次给新的一批，不是把同一页反复念。
        # 这是"别一直刷同一个视频"的第一道防线，第二道是下面的按台账去重。
        videos, source = await bl.bili_feed(limit=12, cfg=cfg)
        record["source"] = source
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"feed:{type(exc).__name__}:{exc}"
        _append_log(cfg, record)
        return record
    if not videos:
        record["skipped"] = "empty_hot"
        _append_log(cfg, record)
        return record
    record["hot"] = len(videos)

    # 去重：2026-10-27 实况，同一个 BV1btH36uEru 一天被挑中 7 次，
    # 日记里堆了四条几乎一样的话。热榜不动，她就会一直点同一行。
    # 所以先把最近刷过的剔掉，再让她挑——连着刷同一个视频，人也不会这么干。
    seen = _recent_bvids(cfg, hours=float(getattr(cfg, "bili_browse_dedupe_hours", 12) or 12))
    fresh = [v for v in videos if str(getattr(v, "bvid", "")) not in seen]
    record["dedupe"] = len(videos) - len(fresh)
    if fresh:
        videos = fresh
    elif seen:
        # 整榜都刷过了：这次就不刷，别拿旧的重播一遍
        record["skipped"] = "all_seen"
        _append_log(cfg, record)
        return record

    listing = "\n".join(
        f"{i}. {v.title[:46]}（{getattr(v, 'owner', '') or ''}）{getattr(v, 'desc', '')[:50]}"
        for i, v in enumerate(videos, 1)
    )
    pick = first_json(await ask_json(cfg, _PICK_INSTRUCTION, listing, llm=llm))
    index = int(pick.get("pick") or 0)
    record["pick"] = index
    record["why"] = str(pick.get("why") or "")[:30]
    if index < 1 or index > len(videos):
        # 一个都不想看是完全正常的事：不感兴趣就不看，别硬刷
        record["skipped"] = "no_interest"
        _append_log(cfg, record)
        return record

    video = videos[index - 1]
    record["bvid"] = video.bvid
    record["title"] = video.title[:60]
    try:
        detail = await bl.bili_video(video.bvid, cfg=cfg, comment_limit=5)
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"video:{type(exc).__name__}:{exc}"
        _append_log(cfg, record)
        return record

    # 播放心跳：B 站历史只认播放器上报。不上报的话，主人那边看不出她刷过什么，
    # 观看时长也不算经验（她卡在 Lv1 就是卡在这）。
    if bool(getattr(cfg, "bili_watch_report_enable", False)):
        try:
            watch = await bl.bili_watch_report(
                video.bvid,
                cfg=cfg,
                seconds=int(getattr(cfg, "bili_watch_report_seconds", 0) or 0),
                verify=bool(getattr(cfg, "bili_watch_report_verify", True)),
            )
            record["watch_played"] = watch.get("played")
            record["watch_in_history"] = watch.get("in_history")
        except Exception as exc:  # noqa: BLE001 — 报不上不该挡住她继续刷
            record["watch_error"] = f"{type(exc).__name__}: {exc}"[:120]

    # 看一眼画面：抽 8 帧给视觉模型，拿到「在干什么」和画面上的字。
    # 抽帧失败/没装 ffmpeg 都只是少一段信息，不影响后面照常刷。
    vision: dict[str, Any] = {}
    if bool(getattr(cfg, "video_vision_enable", False)):
        try:
            from .video_vision import describe_video

            play = await bl.bili_play_url(video.bvid, cfg=cfg)
            vision = await describe_video(
                str(play.get("url") or ""),
                cfg=cfg,
                duration=float(play.get("duration") or 0),
                count=int(getattr(cfg, "video_watch_frames", 8) or 8),
                title=video.title,
                referer="https://www.bilibili.com/",
            )
            record["vision_frames"] = vision.get("frames")
            record["vision_error"] = vision.get("error", "")
        except Exception as exc:  # noqa: BLE001
            vision = {}
            record["vision_error"] = f"{type(exc).__name__}: {exc}"[:120]

    desc = str(getattr(detail, "desc", "") or "")[:800]
    hot_comments = [
        f"{user}：{text[:60]}" for user, text, _up in (getattr(detail, "comments", []) or [])[:5]
    ]
    payload = f"标题：{video.title}\n简介：{desc}\n热评：\n" + "\n".join(hot_comments)
    if vision.get("scene"):
        # 抽帧看到的东西单独标出来，别和字幕混成一段——她要知道哪句是自己"看"见的
        payload += (
            f"\n画面（我抽帧看到的）：{vision.get('scene', '')}"
            + (f"\n画面上的字：{vision.get('text')}" if vision.get("text") else "")
            + (f"\n风格：{vision.get('style')}" if vision.get("style") else "")
        )
    record["desc_chars"] = len(desc)

    # 字幕：这是「她到底知不知道视频讲了什么」的唯一来源（模型看不了画面）。
    # 没有字幕不算失败，退回只看标题简介；读字幕出错也照样往下走。
    if bool(getattr(cfg, "bili_browse_transcript_enable", True)):
        try:
            script = await bl.bili_transcript(
                video.bvid,
                cfg=cfg,
                max_chars=int(getattr(cfg, "bili_transcript_max_chars", 2000) or 2000),
            )
        except Exception as exc:  # noqa: BLE001
            script = ""
            record["transcript_error"] = f"{type(exc).__name__}: {exc}"[:120]
        record["transcript_chars"] = len(script)
        if script:
            payload = (
                payload
                + "\n字幕（机器听写的，可能有错字，别逐字引用）：\n"
                + script
            )

    # 评论：主人要的是「刷了就评论」，所以这一步**不掷骰子**、也不受日记骰子影响。
    # 没写评论不是因为概率，只可能是因为写开关关着、或者号还没到 Lv2。
    if bool(getattr(cfg, "bili_browse_comment_enable", True)) and bool(
        getattr(cfg, "bili_write_enable", False)
    ):
        try:
            from .write_guard import check_can_comment, record_comment

            spoken = first_json(await ask_json(cfg, _COMMENT_INSTRUCTION, payload, llm=llm))
            ctext = str(spoken.get("text") or "").strip()
            if not ctext:
                record["comment_skipped"] = "no_text"
            else:
                blocked = check_can_comment(video.bvid, ctext, cfg, platform="bili")
                if blocked:
                    record["comment_skipped"] = blocked
                else:
                    sent = await bl.bili_comment(video.bvid, ctext, cfg=cfg)
                    record["commented"] = ctext
                    record_comment(video.bvid, ctext, cfg, title=video.title[:60], platform="bili")
                    record["rpid"] = sent.get("rpid")
        except Exception as exc:  # noqa: BLE001 — 发不出去要说清原因，但别让整轮崩掉
            # 最常见的两种：登录态失效、Lv1 未到 Lv2。原样写进台账，日志里一眼能看出
            record["comment_error"] = f"{type(exc).__name__}: {exc}"[:200]
    elif bool(getattr(cfg, "bili_browse_comment_enable", True)):
        record["comment_skipped"] = "write_disabled"

    # 一次模型调用同时问三件事：写不写日记、值不值得长记、要不要收藏。
    # 问在前面是因为点赞/收藏不该被日记骰子牵连——骰子只管"留不留下文字"。
    decision = first_json(await ask_json(cfg, _NOTE_INSTRUCTION, payload, llm=llm))
    note = str(decision.get("note") or "").strip()
    record["keep"] = bool(decision.get("keep"))

    # 点赞 / 收藏：这是真实账号行为，所以要按日限额、同一条不重复点。
    # 为什么点赞不用模型判断：主人要的是"刷到的就点赞"，点赞几乎没有表达成本；
    # 但收藏有——收藏等于说"我以后还会回来看"，那得她自己的判断。
    await _react(cfg, video, decision, record)

    # 骰子只决定"要不要留下文字"，不影响上面两个动作
    probability = float(getattr(cfg, "bili_browse_diary_probability", 0.4) or 0)
    if random.random() > max(0.0, min(1.0, probability)):
        record["skipped"] = "dice"
        _append_log(cfg, record)
        return record

    if not decision.get("keep") or not note:
        record["skipped"] = "nothing_to_say"
        _append_log(cfg, record)
        return record

    # 日记：不碰记忆账本，也不额外生成"她今天干了什么"之外的任何东西
    if bool(getattr(cfg, "diary_enable", True)):
        try:
            from .diary import get_diary_store

            # 目录从配置取（不传就直接走 diary 的单例，跟其它写日记的地方同一本）
            get_diary_store(getattr(cfg, "diary_dir", None) or None).add(
                {
                    "text": note[:120],
                    "title": f"刷到「{video.title[:24]}」",
                    "tags": ["B站"],
                }
            )
            record["diary"] = note[:60]
        except Exception as exc:  # noqa: BLE001 — 日记写失败不影响"看过了"这件事
            record["diary_error"] = f"{type(exc).__name__}:{exc}"
    # 长期记忆：只收模型明确说"值得记住"的那些，宁缺勿滥
    if decision.get("remember"):
        try:
            from .bili_memory import remember

            memo, why = remember(
                cfg,
                bvid=video.bvid,
                title=video.title,
                note=note,
                topic=str(decision.get("topic") or "").strip(),
                tags=["B站"],
            )
            record["memo"] = memo.note[:60] if memo else f"没记：{why}"
        except Exception as exc:  # noqa: BLE001 — 记不住不该影响"看过了"
            record["memo_error"] = f"{type(exc).__name__}:{exc}"

    _append_log(cfg, record)
    return record


async def bili_browse_loop() -> None:
    """后台循环：按作息自己去 B 站刷一刷（默认关闭）。"""
    from nonebot import logger

    from .config import get_config

    cfg = get_config()
    if not bool(getattr(cfg, "bili_browse_enable", False)):
        return
    logger.info(
        "dl_senpai bili_browse loop started "
        f"interval={getattr(cfg, 'bili_browse_interval_min_sec', 5400)}~"
        f"{getattr(cfg, 'bili_browse_interval_max_sec', 14400)}s "
        f"daily_max={getattr(cfg, 'bili_browse_daily_max', 8)}"
    )
    from .heybox_browse import _in_quiet_hours

    while True:
        daily_max = int(getattr(cfg, "bili_browse_daily_max", 8) or 8)
        gap = _next_gap_seconds(cfg)
        if _count_today(cfg) >= daily_max > 0:
            gap = max(gap, random.randint(3600, 9000))
        await asyncio.sleep(max(60, gap))
        if _in_quiet_hours(cfg) or _count_today(cfg) >= daily_max > 0:
            continue
        _bump_today(cfg)
        try:
            record = await browse_once(cfg)
            if record.get("diary"):
                logger.info(f"dl_senpai bili_browse diary={record['diary']}")
        except Exception as exc:  # noqa: BLE001 — 刷失败不该拖垮循环
            logger.warning(f"dl_senpai bili_browse tick failed: {type(exc).__name__}: {exc}")
