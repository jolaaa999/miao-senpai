"""群风格 / 黑话轻量统计（摘自 MumuBot 群文化学习思路，无 LLM 审核）。"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from threading import Lock

_CN_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,15}")
_STOP = frozenset(
    {
        "这个",
        "那个",
        "什么",
        "怎么",
        "不是",
        "就是",
        "我们",
        "你们",
        "他们",
        "自己",
        "一个",
        "没有",
        "还是",
        "可以",
        "因为",
        "所以",
        "但是",
        "然后",
        "如果",
        "已经",
        "真的",
        "感觉",
        "知道",
        "觉得",
        "现在",
        "今天",
        "明天",
        "昨天",
        "学姐",
        "哈哈",
        "哈哈哈",
        "hhh",
        "the",
        "and",
        "for",
        "you",
        "are",
        "this",
        "that",
        "with",
        "have",
        "just",
    }
)


def _ngrams(s: str, n: int) -> list[str]:
    if len(s) < n:
        return []
    return [s[i : i + n] for i in range(len(s) - n + 1)]


def extract_style_tokens(text: str) -> list[str]:
    text = (text or "").strip()
    if len(text) < 2:
        return []
    out: list[str] = []
    for m in _LATIN.finditer(text):
        tok = m.group(0).lower()
        if tok not in _STOP:
            out.append(tok)
    for m in _CN_RUN.finditer(text):
        run = m.group(0)
        if len(run) <= 4:
            if len(run) >= 2 and run not in _STOP:
                out.append(run)
            continue
        for n in (2, 3, 4):
            for g in _ngrams(run, n):
                if g not in _STOP:
                    out.append(g)
    return out


class GroupStyleStore:
    def __init__(self, data_dir: str | Path, *, max_phrases: int = 200) -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.max_phrases = max(40, int(max_phrases))
        self._lock = Lock()

    def _path(self, group_id: int | str) -> Path:
        return self.data_dir / f"group_{group_id}.json"

    def load_counts(self, group_id: int | str) -> Counter[str]:
        path = self._path(group_id)
        if not path.is_file():
            return Counter()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return Counter()
        phrases = raw.get("phrases") if isinstance(raw, dict) else None
        if not isinstance(phrases, dict):
            return Counter()
        counts: Counter[str] = Counter()
        for k, v in phrases.items():
            if isinstance(k, str) and isinstance(v, (int, float)) and v > 0:
                counts[k] = int(v)
        return counts

    def save_counts(self, group_id: int | str, counts: Counter[str]) -> None:
        # 只保留高频
        trimmed = Counter(dict(counts.most_common(self.max_phrases)))
        path = self._path(group_id)
        payload = {"phrases": dict(trimmed)}
        with self._lock:
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def observe(self, group_id: int | str, text: str) -> None:
        tokens = extract_style_tokens(text)
        if not tokens:
            return
        counts = self.load_counts(group_id)
        counts.update(tokens)
        self.save_counts(group_id, counts)

    def top_phrases(
        self,
        group_id: int | str,
        *,
        top_n: int = 8,
        min_count: int = 3,
    ) -> list[tuple[str, int]]:
        counts = self.load_counts(group_id)
        items = [(p, c) for p, c in counts.most_common(top_n * 3) if c >= min_count]
        return items[: max(1, top_n)]


def build_group_style_brief(
    store: GroupStyleStore,
    group_id: int | str,
    *,
    top_n: int = 8,
    min_count: int = 3,
    short: bool = False,
) -> str:
    tops = store.top_phrases(group_id, top_n=top_n, min_count=min_count)
    if not tops:
        return ""
    shown = tops[: 4 if short else top_n]
    phrases = "、".join(p for p, _ in shown)
    extra = "接话时可轻度用，别硬堆。" if not short else "偶尔带一点即可。"
    return f"【本群常说】{phrases}\n{extra}"


_store: GroupStyleStore | None = None
_store_dir: str | None = None


def get_group_style_store(data_dir: str | Path) -> GroupStyleStore:
    global _store, _store_dir
    path = str(data_dir)
    if _store is None or _store_dir != path:
        _store = GroupStyleStore(path)
        _store_dir = path
    return _store
