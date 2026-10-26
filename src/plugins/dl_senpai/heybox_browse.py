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

看完正文，判断她会不会想在评论区说一句。
- 想说的是**短的一句两句**，像真人随手打字：接梗、补充经验、吐槽半句、问一句
- 不要总结全文，不要夸作者，不要"感谢分享"，不要写成小作文
- 不提问式套近乎，不带链接、不带联系方式
- 如果没什么好说的，就别说——多半时候是没什么好说的

只输出一行 JSON：
{"comment": true/false, "text": "<要说的话，不想说就留空>"}"""


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


def _in_quiet_hours(cfg: Any, now: datetime | None = None) -> bool:
    """深夜不刷：跟她自己发消息的静默时段保持一致。"""
    hour = (now or datetime.now()).hour
    start = int(getattr(cfg, "own_life_quiet_start", 3) or 0)
    end = int(getattr(cfg, "own_life_quiet_end", 8) or 0)
    if start == end:
        return False
    return start <= hour < end if start < end else (hour >= start or hour < end)


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
    try:
        doc = await hs.heybox_read(post.link_id, cfg=cfg)
        text = (getattr(doc, "text", "") or "").strip()
    except Exception as exc:  # noqa: BLE001
        record["error"] = f"read:{type(exc).__name__}:{exc}"
        _append_log(cfg, record)
        return record
    record["read_chars"] = len(text)
    if len(text) < 200:
        record["skipped"] = "empty_text"
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
    if not wants or not comment_text:
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
    """后台循环：隔一阵让她自己去逛逛。

    间隔是随机的（默认 1~3 小时）：固定间隔是最容易被识别的机器特征。
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
        f"p_comment={getattr(cfg, 'heybox_browse_comment_probability', 0.35)}"
    )
    while True:
        low = int(getattr(cfg, "heybox_browse_interval_min_sec", 3600) or 3600)
        high = int(getattr(cfg, "heybox_browse_interval_max_sec", 10800) or 10800)
        await asyncio.sleep(random.randint(max(60, min(low, high)), max(low, high)))
        try:
            record = await browse_once(cfg)
            if record.get("commented"):
                logger.info(f"dl_senpai heybox_browse commented link={record.get('link_id')}")
        except Exception as exc:  # noqa: BLE001 — 逛失败不该拖垮循环
            logger.warning(f"dl_senpai heybox_browse tick failed: {type(exc).__name__}: {exc}")
