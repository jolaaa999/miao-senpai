"""把「她自己刷到的东西」喂回她的日常，而不是只躺在文件里。

为什么要有这一层
--------------
刷帖、记日记、记知识点，如果只落盘，对她是没有影响的：下一轮聊天她还是那个
不知道任何事的学姐。主人要的是「刷到的会不断影响她」，所以这里把最近刷到的
内容压成一小段，挂进每轮提示词。

只挑最近 48 小时、最多 4 条，理由跟日记一样：喂多了她每轮都在回味旧事，
接不上眼前的话。

为什么要写「别念出来」
--------------------
模型拿到这种清单，最爱干的事就是逐条复述（我昨天看了A，还看了B，C也…）。
那不是受影响，那是播报。所以这段话明确要求：用得上提一句，用不上别硬塞。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

__all__ = ["build_browse_brief", "recent_items"]

_HEADER = "【我最近自己刷到的】下面这些是我真看过/记住的，不是现编的："
_FOOTER = "用得上就顺口提一句，用不上就别硬塞；别把这段原文念出来。"


def _log_path(cfg: Any, name: str) -> Path:
    raw = str(getattr(cfg, f"{name}_log_file", "") or "")
    if raw:
        return Path(raw)
    return Path(f"data/dl_senpai/{name}_log.jsonl")


def _heybox_rows(cfg: Any, since: float) -> list[dict]:
    path = _log_path(cfg, "heybox_browse")
    return _browse_rows(path, since)


def _browse_rows(path: Path, since: float) -> list[dict]:
    """把任意一份"刷帖台账"读成行，跳过时间窗之外的。"""
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if float(row.get("ts") or 0) >= since and row.get("title"):
            rows.append(row)
    return rows


def recent_items(cfg: Any, *, hours: float = 48.0, limit: int = 4) -> list[str]:
    """最近刷到的东西，按时间倒序，最多 limit 条。"""
    since = time.time() - max(1.0, hours) * 3600
    picked: list[tuple[float, str]] = []

    # B站：记忆本是「她真记住的」，价值最高，优先
    try:
        from . import bili_memory as bm

        for memo in bm.load(cfg):
            if memo.ts < since:
                continue
            topic = f"[{memo.topic}] " if memo.topic else ""
            picked.append((memo.ts, f"B站《{memo.title[:26]}》{topic}——{memo.note[:50]}"))
    except Exception:  # noqa: BLE001 — 记忆本读不出来不该挡聊天
        pass

    # 小黑盒：评过的那条最值得提（她自己说过话），只是看过的次之
    for row in _heybox_rows(cfg, since):
        title = str(row.get("title") or "")[:26]
        said = str(row.get("commented") or "")[:30]
        if said:
            text = f"小黑盒《{title}》——我在下面说过「{said}」"
        elif row.get("read_chars"):
            text = f"小黑盒《{title}》——看过，没说话"
        else:
            continue
        picked.append((float(row.get("ts") or 0), text))

    # 抖音：只有描述，没有"说过话"这一层（评论还没开），所以只列看过什么
    for row in _browse_rows(_log_path(cfg, "douyin_browse"), since):
        title = str(row.get("title") or "")[:26]
        if not title:
            continue
        picked.append((float(row.get("ts") or 0), f"抖音《{title}》——刷到过"))

    picked.sort(key=lambda item: item[0], reverse=True)
    return [text for _, text in picked[: max(1, limit)]]


def build_browse_brief(cfg: Any) -> str:
    """拼成一小段注入提示词。没东西可讲就返回空串。"""
    if not bool(getattr(cfg, "browse_influence_enable", True)):
        return ""
    try:
        items = recent_items(
            cfg,
            hours=float(getattr(cfg, "browse_influence_hours", 48) or 48),
            limit=int(getattr(cfg, "browse_influence_max_items", 4) or 4),
        )
    except Exception:  # noqa: BLE001
        return ""
    if not items:
        return ""
    return "\n".join([_HEADER, *[f"- {text}" for text in items], _FOOTER])
