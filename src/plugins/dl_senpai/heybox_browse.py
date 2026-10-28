"""她自己刷帖子：隔一阵去小黑盒首页逛逛，有话说就评一句。

为什么要有这个模块
------------------
只在"有人跟她说话"时才联网的，是工具，不是人。
主人要的是她自己会刷手机：无聊了刷一下、看到有意思的看两眼、
忍不住了回一句。所以这里做的是**她自己的时间线**：

    定时醒来 -> 刷首页信息流 -> 挑一篇真想看的 -> 读正文 -> 想说才说

怎么防止变成"评论机"
--------------------
1. 挑帖和评不评都是 **LLM 自己判断**，不是脚本按关键词匹配
2. 评论概率（默认 0.35）：十条里挑中的那篇，也只有三分之一会真评
3. 节奏护栏（write_guard）：两条之间至少 10 分钟、同帖不重复、文案不像复制粘贴
4. 被平台挡了就进冷却（heybox 层），不硬冲
5. 全程写台账（jsonl），主人随时能翻她跟谁说了什么

代价要说清楚：即使这些都做到了，自动评论在小黑盒这类社区仍然有被判为
垃圾信息的风险，账号是主人的小号。所以默认关闭，开了也只是"低频像人"。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Any

__all__ = ["browse_once", "browse_loop", "recent_log"]

_PICK_INSTRUCTION = """你在替一个 QQ 群里的学姐（真人感、爱打游戏、嘴不太甜）看小黑盒首页。

下面是首页信息流。选一篇她**真的会点开**的帖子——她对游戏、配装、AI、养号、社群梗有兴趣，
对广告、抽奖、纯引战、纯图无字没兴趣。

只输出一行 JSON：
{"pick": <序号，0 表示都不想看>, "why": "<十个字以内的理由>"}

不要解释，不要输出别的东西。"""

_TALK_INSTRUCTION = """你在替一个 QQ 群里的学姐（真人感、爱打游戏、嘴不太甜）看小黑盒帖子。

**既然点开看完了，就在评论区说一句。** 不是每篇都要深刻，随手一句就行：
- 短的一句两句，像真人打字：接梗、补充自己的经验、吐槽半句、问一句
- 不要总结全文，不要夸作者，不要"感谢分享"，不要写成小作文
- 不提问式套近乎，不带链接、不带联系方式

只有这些情况可以不说（comment 给 false）：
- 整帖是广告、抽奖、引战、纯标题党，没有任何可接的东西
- 正文短到你看不出在讲什么

只输出一行 JSON：
{"comment": true/false, "text": "<要说的话>"}"""


_REPLY_INSTRUCTION = """有人在你的评论下面回了一句。你在纠结要不要接话。

读一遍对方说的话，再决定：
- 顺着聊就接一句，一两句够了，别写小作文
- 对方问问题就答，不知道就说不知道
- 观点不一样可以说不同意见，但别吵架、别教育人
- 纯粹是杠精、引战、刷屏、广告，就当没看见（reply 给 false）

只输出一行 JSON：
{"reply": true/false, "text": "<要说的话，不回就留空>"}"""


def _log_path(cfg: Any) -> Path:
    raw = str(getattr(cfg, "heybox_browse_log_file", "") or "")
    return Path(raw or "data/dl_senpai/heybox_browse_log.jsonl")


def _append_log(cfg: Any, record: dict[str, Any]) -> None:
    path = _log_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def recent_log(cfg: Any, limit: int = 10) -> list[dict[str, Any]]:
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


# 一天里的活跃度权重：晚上最密、午休次之、凌晨最稀。
# 为什么不均匀：人是"扎堆刷手机"的，固定间隔反而是最容易识别的机器特征。
# 数值含义是"相对频率"，实际间隔 = 基础间隔 / 权重。
_HOUR_WEIGHT = {
    0: 0.55, 1: 0.5, 2: 0.5,
    3: 0.0, 4: 0.0, 5: 0.0, 6: 0.0, 7: 0.0,   # 静默时段，权重 0 表示不刷
    8: 1.1, 9: 1.3, 10: 1.3, 11: 1.3,
    12: 1.9, 13: 2.0,                            # 午休：手机上最容易一直在看
    14: 1.2, 15: 1.2, 16: 1.2, 17: 1.3,
    18: 1.7, 19: 1.8,
    20: 2.1, 21: 2.2, 22: 2.1, 23: 1.6,          # 晚上是全天高峰
}


def _hour_weight(hour: int) -> float:
    return float(_HOUR_WEIGHT.get(int(hour) % 24, 1.0))


def _schedule_path(cfg: Any):
    from pathlib import Path

    raw = str(getattr(cfg, "heybox_browse_schedule_file", "") or "")
    return Path(raw or "data/dl_senpai/heybox_browse_schedule.json")


def _today_count(cfg: Any) -> int:
    """今天已经逛了几次。跨天自动归零。"""
    import json

    from pathlib import Path

    path: Path = _schedule_path(cfg)
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if str(data.get("date") or "") != today:
        return 0
    return int(data.get("count") or 0)


def _bump_today(cfg: Any) -> int:
    import json

    path = _schedule_path(cfg)
    today = datetime.now().strftime("%Y-%m-%d")
    count = _today_count(cfg) + 1
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"date": today, "count": count}, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass
    return count


def _in_quiet_hours(cfg: Any, now: datetime | None = None) -> bool:
    """深夜不刷：跟她自己发消息的静默时段保持一致。"""
    hour = (now or datetime.now()).hour
    start = int(getattr(cfg, "own_life_quiet_start", 3) or 0)
    end = int(getattr(cfg, "own_life_quiet_end", 8) or 0)
    if start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


def _seconds_until_quiet_end(cfg: Any, now: datetime | None = None) -> float:
    """离静默结束还有多久。已经不在静默期就返回 0。"""
    from datetime import timedelta

    current = now or datetime.now()
    if not _in_quiet_hours(cfg, current):
        return 0.0
    end_hour = int(getattr(cfg, "own_life_quiet_end", 8) or 8)
    target = current.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if target <= current:
        target = target + timedelta(days=1)
    # 加几分钟随机，别每天八点零分零秒准时上线
    return (target - current).total_seconds() + random.randint(120, 900)


def _next_gap_seconds(cfg: Any) -> int:
    """下一次多久之后逛。基础间隔取配置区间，再按当前小时的活跃度压缩/拉长。"""
    low = int(getattr(cfg, "heybox_browse_interval_min_sec", 3600) or 3600)
    high = int(getattr(cfg, "heybox_browse_interval_max_sec", 10800) or 10800)
    low, high = min(low, high), max(low, high)

    # 落在静默期就直接睡到静默结束
    quiet_wait = _seconds_until_quiet_end(cfg)
    if quiet_wait > 0:
        return int(quiet_wait)

    weight = max(0.2, _hour_weight(datetime.now().hour))
    gap = random.uniform(low, high) / weight

    # 偶发连刷：刚放下手机又点开，是人的常态，也是节奏最不像脚本的地方
    if random.random() < float(getattr(cfg, "heybox_browse_burst_probability", 0.12) or 0):
        gap = random.uniform(120, 480)

    # 上限是硬约束：主人要求"每两小时内必须刷一篇"，所以基础间隔再大也不许超过它。
    # 原来写死 5 小时，遇到低谷时段（权重 0.5）会拉出 4 小时空档，等于没做到。
    max_gap = int(getattr(cfg, "heybox_browse_max_gap_sec", 7200) or 7200)
    return int(max(60, min(gap, max(600, max_gap))))


def _first_json(text: str) -> dict[str, Any]:
    """从模型回复里抠第一段 JSON。她偶尔会带废话，抠不出就当空。"""
    raw = (text or "").strip()
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


async def _ask(cfg: Any, instruction: str, data: str, *, llm: Any = None) -> str:
    """要一行 JSON 的内部判断（实现搬到 ask_json，这里保留旧名字）。"""
    from .ask_json import ask_json

    return await ask_json(cfg, instruction, data, llm=llm)


def _first_json(text: str) -> dict[str, Any]:
    from .ask_json import first_json

    return first_json(text)


async def answer_messages(
    cfg: Any, *, llm: Any = None, limit_posts: int = 3
) -> list[dict[str, Any]]:
    """看有没有人回她，有就接话。

    为什么这条不掷骰子：别人跟她说话，不回才是奇怪的那个。
    但仍然受"两条之间至少隔一会儿"和"同一条留言只处理一次"约束，
    否则会变成秒回的机器人。
    """
    from . import heybox as hs
    from .write_guard import check_can_comment, handled_replies, mark_reply_handled

    rows = await hs.heybox_inbox(cfg, limit_posts=limit_posts)
    if not rows:
        return []
    done = handled_replies(cfg)
    outcomes: list[dict[str, Any]] = []
    for row in rows:
        cid = str(row.get("comment_id") or "")
        if not cid or cid in done:
            continue
        prompt = (
            f"帖子：{row.get('title', '')}\n"
            f"我说过：{row.get('my_comment', '')[:80]}\n"
            f"{row.get('user') or '有人'}回我：{row.get('text', '')[:200]}"
        )
        decision = _first_json(await _ask(cfg, _REPLY_INSTRUCTION, prompt))
        wants = bool(decision.get("reply"))
        text = str(decision.get("text") or "").strip()
        if not wants or not text:
            # 决定不回也算处理过：不然下一轮又会被翻出来重新判断
            mark_reply_handled(cfg, cid)
            outcomes.append({"comment_id": cid, "skipped": "no_reply"})
            continue
        blocked = check_can_comment(
            str(row.get("link_id") or ""), text, cfg, allow_same_link=True
        )
        if blocked:
            outcomes.append({"comment_id": cid, "skipped": f"guard:{blocked}"})
            continue
        try:
            result = await hs.heybox_reply(
                str(row.get("link_id") or ""), str(row.get("text") or "")[:12], text, cfg=cfg
            )
        except Exception as exc:  # noqa: BLE001
            outcomes.append({"comment_id": cid, "error": f"{type(exc).__name__}:{exc}"})
            continue
        mark_reply_handled(cfg, cid)
        outcomes.append(
            {
                "comment_id": cid,
                "to": row.get("user", ""),
                "replied": result.text,
                "unverified": getattr(result, "error", ""),
            }
        )
    return outcomes


def _avoid_path(cfg: Any) -> Path:
    """撞过验证的帖子记在哪。"""
    raw = str(getattr(cfg, "heybox_avoid_file", "") or "")
    return Path(raw) if raw else Path("data") / "dl_senpai" / "heybox_avoid.json"


def _avoid_load(cfg: Any) -> dict[str, float]:
    """读避让名单，顺手把过期的扔掉。"""
    path = _avoid_path(cfg)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    ttl = float(getattr(cfg, "heybox_avoid_ttl_sec", 21600) or 21600)
    now = time.time()
    return {k: float(v) for k, v in (data or {}).items() if isinstance(v, (int, float)) and now - float(v) < ttl}


def _avoid_mark(cfg: Any, link_id: str) -> None:
    """把撞了验证的帖子记下来。

    为什么这么做：同一条链接反复撞验证最容易把账号顶死——
    平台看到的是"这个访客一直在刷同一个被拦的页面"。换一篇比硬顶划算。
    """
    if not link_id:
        return
    data = _avoid_load(cfg)
    data[link_id] = time.time()
    path = _avoid_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


async def browse_once(cfg: Any, *, llm: Any = None, force: bool = False) -> dict[str, Any]:
    """刷一次：看首页 -> 挑一篇读 -> 想说就说。

    返回一条记录，写进台账。任何一步失败都只记原因，不抛给调用方——
    她"今天没逛成"不该炸掉机器人。
    """
    from . import heybox as hs
    from .write_guard import check_can_comment, record_comment

    if not bool(getattr(cfg, "heybox_browse_enable", False)) and not force:
        return {"skipped": "browse_disabled"}
    if _in_quiet_hours(cfg) and not force:
        return {"skipped": "quiet_hours"}

    if llm is None:
        from .llm import get_llm

        llm = get_llm(cfg)

    record: dict[str, Any] = {"ts": time.time(), "at": datetime.now().isoformat(timespec="seconds")}

    # 先看有没有人回我之前说的话：留言是对话，优先级比"自己刷着玩"高
    if bool(getattr(cfg, "heybox_browse_reply_enable", True)):
        try:
            answered = await answer_messages(cfg, llm=llm)
            if answered:
                record["answered"] = answered
        except Exception as exc:  # noqa: BLE001 — 看留言失败不影响她继续逛
            record["inbox_error"] = f"{type(exc).__name__}:{exc}"

    try:
        posts = await hs.heybox_feed(cfg=cfg, limit=12)
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"feed:{type(exc).__name__}:{exc}"
        _append_log(cfg, record)
        return record
    record["feed"] = len(posts)
    if not posts:
        record["skipped"] = "empty_feed"
        _append_log(cfg, record)
        return record

    # 撞过验证的帖子短期内不再出现：换个目标比硬顶同一篇划算
    avoid = _avoid_load(cfg)
    if avoid:
        record["avoided"] = sum(1 for p in posts if p.link_id in avoid)
        posts = [p for p in posts if p.link_id not in avoid]
        if not posts:
            record["skipped"] = "all_avoided"
            _append_log(cfg, record)
            return record

    listing = "\n".join(
        f"{i}. {p.title[:50]}（{p.topics[:16]}，{p.comments} 评）{p.desc[:60]}"
        for i, p in enumerate(posts, 1)
    )
    pick = _first_json(await _ask(cfg, _PICK_INSTRUCTION, listing))
    index = int(pick.get("pick") or 0)
    record["pick"] = index
    record["why"] = str(pick.get("why") or "")[:40]
    if index < 1 or index > len(posts):
        record["skipped"] = "no_interest"
        _append_log(cfg, record)
        return record

    post = posts[index - 1]
    record["link_id"] = post.link_id
    record["title"] = post.title[:60]
    # 读正文要重试一次：小黑盒的风控是"闪一下"的，同一秒读是 0 字，
    # 过两秒再读往往就好了。2026-10-27 实测：12:32~15:03 连续 4 次 0 字，
    # 16:12 再读同一篇立刻拿到 1993 字——所以别拿一次失败就认。
    text = ""
    read_error = ""
    for attempt in range(2):
        try:
            doc = await hs.heybox_read(post.link_id, cfg=cfg)
            text = (getattr(doc, "text", "") or "").strip()
        except Exception as exc:  # noqa: BLE001
            read_error = f"read:{type(exc).__name__}:{exc}"
            text = ""
        if len(text) >= 200:
            break
        if attempt == 0:
            import asyncio as _asyncio

            await _asyncio.sleep(2.5)
    record["read_chars"] = len(text)
    if read_error:
        record["error"] = read_error[:200]
    if len(text) < 200:
        # 连着两次都拿不到字，基本就是被风控了：这时候继续按 2 小时的节奏撞
        # 只会越撞越死，标记 blocked 让循环退避（见 browse_loop）。
        record["skipped"] = "empty_text"
        record["blocked"] = True
        _avoid_mark(cfg, str(record.get("link_id") or ""))
        _append_log(cfg, record)
        return record

    probability = float(getattr(cfg, "heybox_browse_comment_probability", 0.35) or 0)
    if random.random() > max(0.0, min(1.0, probability)):
        record["skipped"] = "dice"
        _append_log(cfg, record)
        return record

    decision = _first_json(
        await _ask(
            cfg, _TALK_INSTRUCTION, f"标题：{post.title}\n\n正文：\n{text[:1800]}"
        )
    )
    wants = bool(decision.get("comment"))
    comment_text = str(decision.get("text") or "").strip()
    record["wants_comment"] = wants
    # 主人定的口味是「刷了就说」，所以这里不再让模型的一票否决生效：
    # 只要它给出了文案就发。模型说 false 却没给文案，才算真没什么可说。
    if not comment_text:
        record["skipped"] = "nothing_to_say"
        _append_log(cfg, record)
        return record

    blocked = check_can_comment(post.link_id, comment_text, cfg)
    if blocked:
        record["skipped"] = f"guard:{blocked}"
        _append_log(cfg, record)
        return record

    try:
        result = await hs.heybox_comment(post.link_id, comment_text, cfg=cfg)
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"comment:{type(exc).__name__}:{exc}"
        _append_log(cfg, record)
        return record
    record["commented"] = result.text
    # 回读确认失败时也要记下来：否则台账里"发过了"和"大概发过了"分不清
    if getattr(result, "error", ""):
        record["comment_unverified"] = result.error
    record_comment(post.link_id, result.text, cfg, title=post.title[:60])
    _append_log(cfg, record)
    return record


async def browse_loop() -> None:
    """后台循环：按人的作息去逛，不是掐着秒表去逛。

    频率靠三层叠出来，而不是把一个固定间隔调小：
    1. 小时权重（_HOUR_WEIGHT）：晚上/午休密，凌晨不刷
    2. 偶发连刷：小概率"刚放下又点开"
    3. 每日上限 + 撞验证冷静期：把总量封死，出事自动退避
    """
    from nonebot import logger

    from .config import get_config

    cfg = get_config()
    if not bool(getattr(cfg, "heybox_browse_enable", False)):
        return
    logger.info(
        "dl_senpai heybox_browse loop started "
        f"interval={getattr(cfg, 'heybox_browse_interval_min_sec', 3600)}~"
        f"{getattr(cfg, 'heybox_browse_interval_max_sec', 10800)}s "
        f"daily_max={getattr(cfg, 'heybox_browse_daily_max', 16)} "
        f"p_comment={getattr(cfg, 'heybox_browse_comment_probability', 0.35)}"
    )
    while True:
        daily_max = int(getattr(cfg, "heybox_browse_daily_max", 16) or 16)
        gap = _next_gap_seconds(cfg)

        # 今天已经逛够了：直接睡到明天，别靠"再想想"来刹车
        if _today_count(cfg) >= daily_max > 0:
            gap = max(gap, random.randint(3600, 9000))

        await asyncio.sleep(max(60, gap))

        if _in_quiet_hours(cfg):
            continue
        if _today_count(cfg) >= daily_max > 0:
            continue

        _bump_today(cfg)
        try:
            record = await browse_once(cfg)
            if record.get("commented"):
                logger.info(f"dl_senpai heybox_browse commented link={record.get('link_id')}")
            if record.get("blocked"):
                # 读不到字 = 被风控了。退避多久由 heybox.note_captcha 决定：
                # 第一次 25 分钟，连着第二次才 3 小时（原来一律 3 小时，一天睡掉一半）。
                from . import heybox as _hb

                backoff = int(_hb.read_cooldown_remaining() or 0) or int(
                    getattr(cfg, "heybox_captcha_backoff_sec", 10800) or 10800
                )
                logger.warning(
                    f"dl_senpai heybox_browse blocked (read_chars={record.get('read_chars')}) "
                    f"backoff {backoff}s"
                )
                await asyncio.sleep(max(600, backoff))
        except Exception as exc:  # noqa: BLE001 — 逛失败不该拖垮循环
            text = f"{type(exc).__name__}: {exc}"
            logger.warning(f"dl_senpai heybox_browse tick failed: {text}")
            # 撞上人机验证/被限流就冷静一阵：这种时候继续重试只会越试越死
            if "人机验证" in text or "show_captcha" in text or "HeyboxError" in text:
                from . import heybox as _hb2

                backoff = int(_hb2.note_captcha() if _hb2.read_cooldown_remaining() <= 0 else _hb2.read_cooldown_remaining())
                logger.warning(f"dl_senpai heybox_browse backoff {backoff}s")
                await asyncio.sleep(max(600, backoff))
