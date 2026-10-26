"""B站留言：看「回复我的」，想接话就回一句。

为什么要单独一个循环
--------------------
B 站跟小黑盒不一样：留言是**官方消息接口**（x/msgfeed/reply），
一次 GET 就拿到，不用开浏览器、不用翻评论页。所以可以查得比小黑盒勤
（默认半小时），也不会把人搞得被限流。

它只做两件事：
    1. 拉留言 -> 去掉处理过的 -> 让 LLM 判断要不要接话、接什么
    2. 要接就发（走 write_guard 的节奏护栏 + 台账）

代价说清楚：B 站对**账号行为**的风控比小黑盒硬，新号、低等级、短时间连发
都容易 -412。所以默认关，开了也只是"半小时看一次、一次最多两条"。
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any

__all__ = ["answer_once", "bili_inbox_loop"]

_REPLY_INSTRUCTION = """有人在 B 站回了你的评论。你在纠结要不要接话。

读一遍对方说的话，再决定：
- 顺着聊就接一句，一两句够了，别写小作文
- 对方问问题就答，不知道就说不知道
- 观点不一样可以说不同意见，但别吵架、别教育人
- 纯杠精、引战、广告、群发内容，就当没看见（reply 给 false）
- 你看不到视频画面，别装作看过

只输出一行 JSON：
{"reply": true/false, "text": "<要说的话，不回就留空>"}"""


async def answer_once(cfg: Any, *, llm: Any = None) -> list[dict[str, Any]]:
    """看一轮留言，能回的回掉。返回这一轮的流水（写日志用）。"""
    from . import bili as bl
    from .ask_json import ask_json, first_json
    from .write_guard import check_can_comment, handled_replies, mark_reply_handled

    rows = await bl.bili_messages(cfg=cfg, limit=20)
    if not rows:
        return []
    done = handled_replies(cfg, "bili")
    budget = int(getattr(cfg, "bili_inbox_max_per_tick", 2) or 2)
    outcomes: list[dict[str, Any]] = []
    answered = 0
    for row in rows:
        if not row.replyable:
            continue
        key = row.message_id
        if key in done:
            continue
        if answered >= budget:
            # 一次醒来最多回这么多：连回一串是最像机器人的动作
            break
        prompt = (
            f"我说过：{row.my_text[:80] or '（不知道原话）'}\n"
            f"{row.user or '有人'}回我：{row.text[:200]}\n"
            f"视频：{row.bvid or 'av' + str(row.oid)}"
        )
        decision = first_json(await ask_json(cfg, _REPLY_INSTRUCTION, prompt, llm=llm))
        if not decision.get("reply") or not str(decision.get("text") or "").strip():
            mark_reply_handled(cfg, key, platform="bili")
            outcomes.append({"id": key, "skipped": "no_reply"})
            continue
        text = str(decision.get("text") or "").strip()
        blocked = check_can_comment(
            row.bvid or f"av{row.oid}", text, cfg, allow_same_link=True, platform="bili"
        )
        if blocked:
            outcomes.append({"id": key, "skipped": f"guard:{blocked}"})
            continue
        try:
            result = await bl.bili_comment(
                row.bvid or f"av{row.oid}",
                text,
                cfg=cfg,
                root_id=row.root_id,
                parent_id=row.parent_id,
            )
        except Exception as exc:  # noqa: BLE001 — 单条失败不该拖垮整轮
            outcomes.append({"id": key, "error": f"{type(exc).__name__}: {exc}"})
            continue
        mark_reply_handled(cfg, key, platform="bili")
        answered += 1
        outcomes.append(
            {
                "id": key,
                "to": row.user,
                "replied": text,
                "rpid": result.get("rpid"),
            }
        )
    return outcomes


def _log_path(cfg: Any):
    from pathlib import Path

    raw = str(getattr(cfg, "bili_inbox_log_file", "") or "")
    return Path(raw or "data/dl_senpai/bili_inbox_log.jsonl")


def _append_log(cfg: Any, record: dict[str, Any]) -> None:
    import json

    path = _log_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


async def bili_inbox_loop() -> None:
    """后台循环：隔一阵看一次 B 站留言。"""
    from nonebot import logger

    from .config import get_config

    cfg = get_config()
    if not bool(getattr(cfg, "bili_inbox_enable", False)):
        return
    interval = int(getattr(cfg, "bili_inbox_interval_sec", 1800) or 1800)
    logger.info(f"dl_senpai bili_inbox loop started interval={interval}s")
    # 启动后先等一个完整间隔：机器人刚起来时不该立刻去动别人的账号
    await asyncio.sleep(min(interval, 300))
    while True:
        record: dict[str, Any] = {"ts": time.time(), "at": datetime.now().isoformat(timespec="seconds")}
        try:
            outcomes = await answer_once(cfg)
            if outcomes:
                record["outcomes"] = outcomes
                _append_log(cfg, record)
                for row in outcomes:
                    if row.get("replied"):
                        logger.info(f"dl_senpai bili.inbox replied to={row.get('to')}")
        except Exception as exc:  # noqa: BLE001 — 看留言失败不该拖垮循环
            record["error"] = f"{type(exc).__name__}: {exc}"
            _append_log(cfg, record)
            logger.warning(f"dl_senpai bili_inbox tick failed: {type(exc).__name__}: {exc}")
        await asyncio.sleep(max(300, interval))
