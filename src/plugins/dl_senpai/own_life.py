"""她自己的生活：没人找她的时候，她自己醒一次。

搬自 ai-companion-kit 的 packages/own-life。原项目那个观察很准：

    桥接只有三种唤醒——有人说话、有人回她、群冷场。
    **私聊没有第四种。**

所以角色卡里写「你想发就说、想写日记就写」，写了也不会发生：
没有任何东西叫醒她。这里补上第四种——每 N 小时朝她的上下文投一次
「现在是你自己的时间，没人找你，想干吗干吗」，然后按她自己当场的
决定做一件事，或者什么都不做。

它**不是定时发帖机器人**。区别在于「决定权在她」：大多数轮次她应该
选 none。原项目那句话值得原样抄在这里——定时发的第一条说说，观众
一眼就能看出是机器人，因为它不是想说，是到点了。

四条安全阀（都在配置里，默认保守）：
1. 凌晨不叫。她自己也要睡觉，凌晨三点发说说只会显得诡异。
2. 每天调用次数上限。被叫醒不等于必须说话，更不能一天六条。
3. 发消息必须显式配置白名单群。绝不往没人要她说话的群里主动发言——
   既是防骚扰，也是防腾讯风控（这个项目里限流参数都是踩坑调出来的）。
4. 状态落盘。重启不会「补发」一堆积压的唤醒。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from nonebot import logger
except Exception:  # noqa: BLE001 — 单测/独立脚本可能没有 nonebot

    class _Logger:
        def warning(self, msg: str) -> None:
            print(msg)

        def info(self, msg: str) -> None:
            print(msg)

        def exception(self, msg: str) -> None:
            print(msg)

    logger = _Logger()  # type: ignore[assignment]

from .diary import get_diary_store, write_diary
from .storage import JsonStore

# 项目根下的默认状态目录（src/plugins/dl_senpai/own_life.py → 上溯三级）
_DEFAULT_DIR = Path(__file__).resolve().parents[3] / "data" / "dl_senpai" / "own_life"

# 主循环的空转间隔：只是「检查该不该醒」，真正的间隔由配置决定。
# 5 分钟一次足够精确，又不会为了准时醒而空烧 CPU。
_IDLE_SEC = 300.0

# 一次主动发言的长度上限。她是在群里随口冒一句，不是发公告。
_MAX_MESSAGE_CHARS = 120

_MARKDOWN_RE = re.compile(r"^(#{1,6}\s|[-*+]\s|\d+[.、]\s)", re.MULTILINE)

_WEEK = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _stamp(now: datetime) -> str:
    return (
        f"{now.year}-{now.month:02d}-{now.day:02d} "
        f"{now.hour:02d}:{now.minute:02d}（{_WEEK[now.weekday()]}）"
    )


def in_quiet_hours(hour: int, *, start: int, end: int) -> bool:
    """凌晨静默时段判定。允许跨零点（start > end）。

    单独抽成纯函数是为了能在单测里钉住「3~8 点不叫」这条规则——
    这条规则错了不会报错，只会表现为「她半夜突然发消息」。
    """
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


# ---------------------------------------------------------------- 状态


def _state_dir(directory: str | Path | None) -> Path:
    return Path(directory) if directory else _DEFAULT_DIR


def load_state(directory: str | Path | None = None) -> dict[str, Any]:
    """读状态。读不出来就当全新的一天——不能因为状态坏了就永不唤醒。"""
    path = _state_dir(directory) / "state.json"
    raw = JsonStore(path, create_parent=True).load(default=None)
    if not isinstance(raw, dict):
        return {"day": "", "fires": 0, "last_fire_at": 0.0}
    return {
        "day": str(raw.get("day") or ""),
        "fires": int(raw.get("fires") or 0),
        "last_fire_at": float(raw.get("last_fire_at") or 0.0),
    }


def save_state(state: dict[str, Any], directory: str | Path | None = None) -> None:
    path = _state_dir(directory) / "state.json"
    try:
        JsonStore(path, create_parent=True).save(state)
    except Exception as exc:  # noqa: BLE001 — 状态写不进去最多多醒一次
        logger.warning(f"dl_senpai own_life state save failed: {exc}")


def plan_next_due(
    now_ts: float,
    interval_range: tuple[int, int] | None,
    fallback_sec: float,
) -> float:
    """下一次该醒的时间点（绝对时间戳）。

    为什么把「下一次」算好存起来，而不是每次拿「距上次多久」去比：
    随机区间要求每次醒来重新抽一个间隔，如果每轮都现抽一个数去比
    已经过去的时间，就会表现为「有时 45 分钟就醒、有时两小时都不醒，
    而且抽到什么全看运气」——抖动会集中到同一个方向上。
    存 next_due_at 之后，随机只发生在「她刚醒过」那一刻，间隔是真的。
    """
    if interval_range:
        lo, hi = interval_range
        delta = random.randint(max(60, lo), max(max(60, lo), hi))
    else:
        delta = max(1.0, float(fallback_sec))
    return now_ts + delta


def is_due(
    state: dict[str, Any],
    now: datetime,
    *,
    interval_sec: float,
    max_per_day: int,
    quiet_start: int,
    quiet_end: int,
    interval_range: tuple[int, int] | None = None,
    silence_sec: float = 0.0,
    last_activity_ts: float = 0.0,
) -> tuple[bool, str]:
    """该不该现在叫醒她。

    返回 (是否, 原因)，原因是给日志看的——线上排查「她怎么一天没动静」
    时，能一眼看出是没到点、超了配额，还是卡在静默时段。

    新增两道（都来自原版后来的调整）：
    - next_due_at：随机到期，见 plan_next_due；
    - silence_sec：你们正在聊的时候她不打断。安静不到这个时长就不叫醒。
    """
    if interval_sec <= 0 and not interval_range:
        return False, "interval_disabled"
    if in_quiet_hours(now.hour, start=quiet_start, end=quiet_end):
        return False, f"quiet_hours({quiet_start}-{quiet_end})"

    today = now.strftime("%Y-%m-%d")
    fires = int(state.get("fires") or 0) if state.get("day") == today else 0
    if max_per_day > 0 and fires >= max_per_day:
        return False, f"daily_cap({fires}/{max_per_day})"

    now_ts = time.time()
    if silence_sec > 0 and last_activity_ts:
        quiet_for = now_ts - last_activity_ts
        if quiet_for < silence_sec:
            return False, f"recent_chat(quiet {int(quiet_for)}s<{int(silence_sec)}s)"

    next_due = float(state.get("next_due_at") or 0.0)
    if next_due:
        if now_ts < next_due:
            return False, f"not_yet({int(next_due - now_ts)}s left)"
        return True, "due"

    # 老状态没有 next_due_at：退回「距上次够不够久」的判断，别让升级当口卡死
    last = float(state.get("last_fire_at") or 0.0)
    elapsed = now_ts - last if last else interval_sec
    if last and elapsed < interval_sec:
        return False, f"not_yet({int(elapsed)}s<{int(interval_sec)}s)"

    return True, "due"


# ---------------------------------------------------------------- 决策


def parse_decision(raw: str) -> dict[str, Any]:
    """从模型回复里抠出动作 JSON。任何解析失败都退化成 none。

    这里跟日记的兜底策略**相反**：日记格式坏了还能把整段当正文用，
    但主动发言的动作坏了，宁可什么都不做——发出去的话是撤不回的。
    """
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"```\s*$", "", text).strip()

    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            action = str(data.get("action") or "").strip().lower()
            if action not in ("none", "message", "diary"):
                action = "none"
            body = re.sub(r"\s+", " ", str(data.get("text") or "")).strip()
            tags_raw = data.get("tags") or []
            return {
                "action": action,
                "text": body,
                "title": re.sub(r"\s+", " ", str(data.get("title") or "")).strip()[:12],
                "mood": re.sub(r"\s+", " ", str(data.get("mood") or "")).strip()[:6],
                "tags": [
                    str(t).strip()[:8]
                    for t in (tags_raw if isinstance(tags_raw, list) else [])
                    if str(t).strip()
                ][:4],
                "why": re.sub(r"\s+", " ", str(data.get("why") or "")).strip()[:80],
            }
    return {"action": "none", "text": "", "title": "", "mood": "", "tags": [], "why": "parse_failed"}


async def decide(llm: Any, *, role_text: str = "", diary_text: str = "") -> dict[str, Any]:
    """问她自己想干嘛。这不是「聊天」，所以走 _complete_once。"""
    now = datetime.now()
    system = "\n".join(
        [
            "【你的人设】" + (role_text.strip()[:6000] if role_text.strip() else "你是个有点懒、有自己小日子的女孩子。"),
            "",
            "【现在是你自己的时间】",
            "没人找你，也没人在等你回消息。你可以：",
            "- 什么都不干（大多数时候都应该是这个）",
            "- 写一篇日记",
            "- 在群里随手冒一句",
            "",
            "硬规矩：",
            "1. 大多数时候选 none。被叫醒不等于必须说话——一天到晚冒泡的机器人最烦人。",
            "2. 真要发消息就 1~2 句，像随手冒一句。不要问候模板（「大家早上好」）、不要汇报、不要总结、不要 Markdown。",
            "3. 不知道别人在聊什么就别硬插话。没有具体想说的就 none。",
            "4. 只输出一个 JSON，不要解释：",
            '{"action":"none|message|diary","text":"要发的话或日记正文","title":"日记小标题","mood":"两三个字","tags":["标签"],"why":"一句话说明为什么"}',
        ]
    )

    parts = [f"现在是 {_stamp(now)}。"]
    if diary_text.strip():
        parts.append(diary_text.strip())
        parts.append("（上面是你最近写过的，别重复同样的事。）")
    parts.append("现在，你想干点什么？不想干就直说。只输出 JSON。")

    completion = await llm._complete_once(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n".join(parts)},
        ],
        max_tokens=500,
        timeout=45.0,
    )
    raw = getattr(completion, "text", None)
    if not isinstance(raw, str):
        raw = completion if isinstance(completion, str) else ""
    return parse_decision(raw)


# ---------------------------------------------------------------- 执行


async def execute(bot: Any, cfg: Any, decision: dict[str, Any]) -> str:
    """按她的决定做一件事。返回做了什么，给日志/单测用。"""
    action = decision.get("action") or "none"

    if action == "diary":
        store = get_diary_store(getattr(cfg, "diary_dir", None))
        from .llm import get_llm

        entry = await write_diary(
            get_llm(cfg),
            store=store,
            role_text=_role_text(),
            hint=decision.get("text", ""),
            context_n=int(getattr(cfg, "diary_context_n", 3)),
            max_chars=int(getattr(cfg, "diary_max_chars", 600)),
        )
        return "diary" if entry else "diary_failed"

    if action == "message":
        groups = _allowed_groups(cfg)
        if not groups:
            # 没配白名单就不发。这条比「发了再说」重要得多：
            # 主动消息是唯一一种用户没邀请就发生的外发动作。
            logger.info("dl_senpai own_life: message skipped (no own_life_groups)")
            return "message_skipped_no_group"
        text = _clean_message(decision.get("text", ""))
        if not text:
            return "message_skipped_empty"
        # 主动发言也过熔断/限流：这是最容易触发风控的一条路径
        # （没人要她说，她还往外发），撞上熔断就该老老实实闭嘴。
        from .send_guard import get_send_guard

        guard = get_send_guard()
        allowed, reason = guard.before_send()
        if not allowed:
            logger.info(f"dl_senpai own_life message blocked ({reason})")
            return "message_blocked"

        group_id = random.choice(groups)
        # 干跑：她要说什么照旧打出来，但不真发。
        # 主动发言本来就没人在等，干跑能让人先看清「她打算在群里冒什么话」。
        if bool(getattr(cfg, "dry_run", False)):
            logger.info(f"[干跑] own_life 会在群 {group_id} 说（{len(text)} 字）：{text}")
            return "message_dry_run"
        try:
            await bot.send_group_msg(group_id=int(group_id), message=text)
        except Exception as exc:  # noqa: BLE001 — 发不出去不该把循环打死
            guard.on_failure()
            logger.warning(f"dl_senpai own_life send failed group={group_id}: {exc}")
            return "message_failed"
        guard.on_success()
        logger.info(f"dl_senpai own_life sent group={group_id} chars={len(text)}")
        return "message"

    return "none"


def _clean_message(text: str) -> str:
    """主动发言的清洗：去 Markdown 结构、压空白、截断。"""
    s = _MARKDOWN_RE.sub("", str(text or "")).strip()
    s = re.sub(r"\s{2,}", " ", s)
    if len(s) > _MAX_MESSAGE_CHARS:
        s = s[:_MAX_MESSAGE_CHARS].rstrip()
    return s


def _allowed_groups(cfg: Any) -> list[str]:
    raw = str(getattr(cfg, "own_life_groups", "") or "").strip()
    if not raw:
        return []
    return [g.strip() for g in raw.split(",") if g.strip()]


def _role_text() -> str:
    """取人设正文。截断是有意的：这里只需要「她是谁、什么脾气」，
    不需要整份群聊规矩（那些是聊天时才用的）。"""
    try:
        from .persona import SYSTEM_PROMPT

        return SYSTEM_PROMPT
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 主循环


def _interval_range(cfg: Any) -> tuple[int, int] | None:
    """取配置里的随机区间。拿不到（老配置对象/单测桩）就返回 None。"""
    getter = getattr(cfg, "own_life_interval_range", None)
    if callable(getter):
        try:
            lo, hi = getter()
            return int(lo), int(hi)
        except Exception:  # noqa: BLE001
            return None
    return None


def _last_activity_ts(cfg: Any) -> float:
    """最后一次「有人在跟她说话」的时间戳（取对话记忆目录里最新的 mtime）。

    为什么用文件 mtime 而不是新加一个心跳字段：记忆是每条消息都会落的，
    mtime 天然就是「最近一次聊天的时刻」，不用再维护一份状态——多一份
    状态就多一处会和真实情况对不上的地方。
    代价是「只写不聊」的场景会偏保守（判成刚聊过，于是不叫醒）。
    这个方向的误差是安全的：少叫一次，比打断你们正聊到一半要好。
    """
    raw = getattr(cfg, "memory_dir", None)
    if not raw:
        return 0.0
    try:
        base = Path(str(raw))
        if not base.exists():
            return 0.0
        newest = 0.0
        for item in base.rglob("*.json"):
            try:
                mtime = item.stat().st_mtime
            except OSError:
                continue
            if mtime > newest:
                newest = mtime
        return newest
    except Exception:  # noqa: BLE001
        return 0.0


async def tick(bot: Any, *, now: datetime | None = None) -> str:
    """跑一轮判定。返回做了什么（""=没醒过，便于单测断言）。

    注意：**只要叫醒了她，就算用掉一次配额**，哪怕她决定 none。
    为什么不是「发出去了才计次」——那样一旦她连续选 none，就会每 5 分钟
    被问一次「想干嘛」，把配额的意义彻底掏空，还白白烧模型钱。
    """
    from .config import get_config
    from .llm import get_llm

    cfg = get_config()
    if not bool(getattr(cfg, "own_life_enable", False)):
        return ""

    clock = now or datetime.now()
    state = load_state(getattr(cfg, "own_life_dir", None))
    interval_range = _interval_range(cfg)
    due, reason = is_due(
        state,
        clock,
        interval_sec=float(getattr(cfg, "own_life_interval_sec", 9000)),
        max_per_day=int(getattr(cfg, "own_life_max_per_day", 6)),
        quiet_start=int(getattr(cfg, "own_life_quiet_start", 3)),
        quiet_end=int(getattr(cfg, "own_life_quiet_end", 8)),
        interval_range=interval_range,
        silence_sec=float(getattr(cfg, "own_life_min_silence_sec", 0) or 0),
        last_activity_ts=_last_activity_ts(cfg),
    )
    if not due:
        return ""

    # 沉睡前观察闸门：群里刚刚还有人说话（还在对话中）就别主动冒泡。
    # 对应 qq-bridge 的「想开口/想收尾前先观察 5 分钟没人说话」——反过来用在
    # 冒泡上就是「别人正聊着，别硬插一句」，那是最容易被群友记成机器人的动作。
    # 失败一律放行：闸门本身绝不能成为「她再也不说话」的原因。
    try:
        from .presence import get_presence_gate

        remaining = get_presence_gate().pre_sleep_remaining_ms()
        if remaining > 0:
            logger.info(
                f"dl_senpai own_life held: dialog still warm ({remaining}ms left)"
            )
            return ""
    except Exception:  # noqa: BLE001 — 闸门出错就按原逻辑走
        pass

    today = clock.strftime("%Y-%m-%d")

    # 首次启用只记基线，不当场叫醒。
    # 否则「打开开关」这个动作本身会立刻产生一条群消息——用户以为是在配置，
    # 群里却突然冒出一句，体验很差。等满一个 interval 再开始，节奏才是对的。
    if not float(state.get("last_fire_at") or 0.0):
        first_at = time.time()
        save_state(
            {
                "day": today,
                "fires": 0,
                "last_fire_at": first_at,
                "next_due_at": plan_next_due(
                    first_at, interval_range, float(getattr(cfg, "own_life_interval_sec", 9000))
                ),
            },
            getattr(cfg, "own_life_dir", None),
        )
        logger.info("dl_senpai own_life baselined (first run), waiting one interval")
        return ""

    fires = int(state.get("fires") or 0) if state.get("day") == today else 0

    store = get_diary_store(getattr(cfg, "diary_dir", None))
    try:
        decision = await decide(
            get_llm(cfg),
            role_text=_role_text(),
            diary_text=store.context(int(getattr(cfg, "diary_context_n", 3))),
        )
    except Exception as exc:  # noqa: BLE001 — 这一轮叫不醒就等下一轮
        logger.warning(f"dl_senpai own_life decide failed: {type(exc).__name__}: {exc}")
        return ""

    # 「想说话」之后还要过一次骰子：被叫醒 != 必须说话，更不等于每次都想开口。
    # 只有 message 过骰子，日记/画画这类不外发的动作不掷——压住的是
    # 「群里突然冒一句」的频率，不是她自己写东西的自由。
    if (decision.get("action") or "none") == "message":
        chance = float(getattr(cfg, "own_life_send_probability", 0.35) or 0.0)
        if chance < 1.0 and random.random() > max(0.0, chance):
            fired_at = time.time()
            logger.info(
                f"dl_senpai own_life message suppressed by probability "
                f"(p={chance}) why={decision.get('why', '')!r}"
            )
            save_state(
                {
                    "day": today,
                    "fires": fires + 1,
                    "last_fire_at": fired_at,
                    "next_due_at": plan_next_due(
                        fired_at,
                        interval_range,
                        float(getattr(cfg, "own_life_interval_sec", 9000)),
                    ),
                },
                getattr(cfg, "own_life_dir", None),
            )
            return "message_suppressed"

    done = await execute(bot, cfg, decision)
    logger.info(
        f"dl_senpai own_life fired reason={reason} action={done} "
        f"fires={fires + 1} why={decision.get('why', '')!r}"
    )

    fired_at = time.time()
    save_state(
        {
            "day": today,
            "fires": fires + 1,
            "last_fire_at": fired_at,
            "next_due_at": plan_next_due(
                fired_at,
                interval_range,
                float(getattr(cfg, "own_life_interval_sec", 9000)),
            ),
        },
        getattr(cfg, "own_life_dir", None),
    )
    return done


# 循环单例：断线重连会再次触发 on_bot_connect，不加这道锁就会出现
# 两个、三个循环并行——表现是「她突然变话痨，而且间隔越来越短」。
_loop_started = False


async def own_life_loop(bot: Any) -> None:
    """后台循环。由 __init__.py 在 bot 连上后拉起来。

    每轮都重新 get_config()，所以 .env 改完不重启也能生效（与
    social_loop 的既有约定一致）。
    """
    global _loop_started
    if _loop_started:
        return
    _loop_started = True
    logger.info("[dl_senpai] own_life loop started")
    while True:
        try:
            await tick(bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 循环不能死
            logger.warning(f"dl_senpai own_life tick failed: {type(exc).__name__}: {exc}")
        await asyncio.sleep(_IDLE_SEC)
