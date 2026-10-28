"""她刷到过、并且真的记住了的东西。

为什么单独一本，不塞进现成的两处
--------------------------------
1. **不是日记**：日记是「我自己的一天」，按天分文件、会分层、会过期；
   这里是「我记住的几个知识点」，要能几个月后被翻出来。
2. **不是人物记忆**：person_memory 是「关于你的事」——他喜欢什么、他在忙什么。
   一条视频讲买车，跟她对主人的了解没有关系，混进去会把人物画像冲淡。

所以这里是一个很小的、带上限的长期清单：只存「她看完真觉得值得记」的东西，
每条带一句自己的话 + 三五个字的主题，能按关键词搜回来。

上限与去重
----------
上限默认 200 条（超了丢最旧的）。去重两条规则，跟写评论那套是同一个思路：
同一条视频不再记第二次；跟她已经记下的话太像（bigram 相似度 0.8 以上）也不记。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "BiliMemo",
    "load",
    "remember",
    "remove",
    "render",
    "search",
    "stats",
]

_DEFAULT_PATH = "data/dl_senpai/bili_memory.json"
_KEEP_MAX = 200
_SIMILAR = 0.8


@dataclass
class BiliMemo:
    """一条她记住的东西。"""

    ts: float = 0.0
    bvid: str = ""
    title: str = ""
    topic: str = ""
    note: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def when(self) -> str:
        import datetime

        if not self.ts:
            return ""
        return datetime.datetime.fromtimestamp(self.ts).strftime("%m-%d")


def _path(cfg: Any) -> Path:
    raw = str(getattr(cfg, "bili_memory_file", "") or "")
    return Path(raw or _DEFAULT_PATH)


def load(cfg: Any) -> list[BiliMemo]:
    path = _path(cfg)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, dict):
        return []
    rows: list[BiliMemo] = []
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        rows.append(
            BiliMemo(
                ts=float(item.get("ts") or 0),
                bvid=str(item.get("bvid") or ""),
                title=str(item.get("title") or ""),
                topic=str(item.get("topic") or ""),
                note=str(item.get("note") or ""),
                tags=[str(t) for t in (item.get("tags") or [])],
            )
        )
    return rows


def _save(cfg: Any, rows: list[BiliMemo]) -> None:
    path = _path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    cap = int(getattr(cfg, "bili_memory_max", _KEEP_MAX) or _KEEP_MAX)
    trimmed = rows[-cap:] if cap > 0 else rows
    payload = {"items": [asdict(row) for row in trimmed]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def _bigrams(text: str) -> set[str]:
    raw = re.sub(r"[\s\W_]+", "", (text or "").lower())
    return {raw[i : i + 2] for i in range(len(raw) - 1)} if len(raw) > 1 else set()


def _too_similar(note: str, others: list[str]) -> bool:
    """跟她已经记下的话有多像。太像就不记：同一件事不该占两个位置。"""
    here = _bigrams(note)
    if not here:
        return False
    for other in others:
        there = _bigrams(other)
        if not there:
            continue
        shared = len(here & there) / len(here | there)
        if shared >= _SIMILAR:
            return True
    return False


def remember(
    cfg: Any,
    *,
    bvid: str,
    title: str,
    note: str,
    topic: str = "",
    tags: list[str] | None = None,
) -> tuple[BiliMemo | None, str]:
    """记一条。返回 (记下的条目, 没记的原因)；记下了原因就是空串。"""
    text = (note or "").strip()
    if not text:
        return None, "空内容不记"
    rows = load(cfg)
    if bvid and any(row.bvid == bvid for row in rows):
        return None, "这条视频已经记过了"
    if _too_similar(text, [row.note for row in rows]):
        return None, "跟已经记下的话太像"
    memo = BiliMemo(
        ts=time.time(),
        bvid=bvid,
        title=(title or "").strip()[:80],
        topic=(topic or "").strip()[:16],
        note=text[:160],
        tags=[str(t) for t in (tags or [])][:6],
    )
    rows.append(memo)
    _save(cfg, rows)
    return memo, ""


def remove(cfg: Any, *, bvid: str = "", index: int = 0) -> bool:
    """删一条（她说「这个我记错了」的时候用）。"""
    rows = load(cfg)
    keep: list[BiliMemo] = []
    dropped = False
    for i, row in enumerate(rows, 1):
        if (bvid and row.bvid == bvid) or (index and i == index):
            dropped = True
            continue
        keep.append(row)
    if dropped:
        _save(cfg, keep)
    return dropped


def search(cfg: Any, query: str = "", *, limit: int = 5) -> list[BiliMemo]:
    """按主题/正文/标题粗搜。空查询就是「最近记的」。"""
    rows = load(cfg)
    key = (query or "").strip().lower()
    if not key:
        return rows[::-1][: max(1, limit)]
    words = [w for w in re.split(r"[\s,，、]+", key) if w]
    hits: list[BiliMemo] = []
    for row in rows[::-1]:
        blob = f"{row.topic} {row.title} {row.note} {" ".join(row.tags)}".lower()
        if all(word in blob for word in words):
            hits.append(row)
        if len(hits) >= max(1, limit):
            break
    return hits


def stats(cfg: Any) -> dict[str, Any]:
    rows = load(cfg)
    return {"count": len(rows), "latest": rows[-1].note[:40] if rows else ""}


def render(rows: list[BiliMemo], *, limit: int = 6) -> str:
    """排成能读的样子：她自己在对话里要引用这些。"""
    if not rows:
        return "我这儿还没记住什么B站的东西。"
    lines = [f"我记住的B站内容（{len(rows)} 条）："]
    for i, row in enumerate(rows[:limit], 1):
        lines.append(f"{i}. [{row.topic or '杂'}] {row.note}")
        if row.title:
            lines.append(f"   （《{row.title[:30]}》 {row.when} {row.bvid}）")
    if len(rows) > limit:
        lines.append(f"（还有 {len(rows) - limit} 条）")
    return "\n".join(lines)
