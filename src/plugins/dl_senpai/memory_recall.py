"""会话记忆关键词召回（轻量，无 embedding）。"""

from __future__ import annotations

import re
from typing import Any

_CN_RUN = re.compile(r"[\u4e00-\u9fff]+")
_LATIN = re.compile(r"[A-Za-z0-9_]{3,}")
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
        "学姐",
        "哈哈",
        "谢谢",
        "记得",
        "还记",
    }
)


def _ngrams(s: str, n: int) -> list[str]:
    if len(s) < n:
        return []
    return [s[i : i + n] for i in range(len(s) - n + 1)]


def extract_keywords(text: str) -> set[str]:
    text = (text or "").strip().lower()
    if not text:
        return set()
    out: set[str] = set()
    for m in _LATIN.finditer(text):
        tok = m.group(0)
        if tok not in _STOP and not tok.isdigit():
            out.add(tok)
    for m in _CN_RUN.finditer(text):
        run = m.group(0)
        if len(run) <= 4:
            if len(run) >= 2 and run not in _STOP:
                out.add(run)
            continue
        for n in (2, 3, 4):
            for g in _ngrams(run, n):
                if g not in _STOP:
                    out.add(g)
    return out


def score_text(text: str, keywords: set[str]) -> float:
    if not keywords or not text:
        return 0.0
    lower = text.lower()
    hits = sum(1 for k in keywords if k in lower)
    if hits <= 0:
        return 0.0
    # 短句命中更「专」
    length_penalty = 1.0 / (1.0 + len(text) / 400.0)
    return hits * (1.0 + 0.15 * hits) * length_penalty


def select_recent_and_recall(
    history: list[dict[str, str]],
    query: str,
    *,
    recent_turns: int = 24,
    recall_top: int = 4,
) -> tuple[list[dict[str, str]], str]:
    """返回 (近期 history 给 messages, 召回 brief)。

    history 是 user/assistant 交替消息列表（不含当前句）。
    recent_turns 按「消息条数」计（不是对话轮数对）。
    """
    if not history:
        return [], ""
    recent_n = max(2, int(recent_turns))
    recall_n = max(0, int(recall_top))
    if len(history) <= recent_n or recall_n <= 0:
        return history[-recent_n:] if len(history) > recent_n else list(history), ""

    recent = history[-recent_n:]
    older = history[:-recent_n]
    keywords = extract_keywords(query)
    if not keywords:
        return recent, ""

    # 按「一对」或单条打分，优先 assistant+前面的 user
    scored: list[tuple[float, int, str]] = []
    for i, msg in enumerate(older):
        content = str(msg.get("content") or "")
        role = str(msg.get("role") or "")
        sc = score_text(content, keywords)
        if sc <= 0:
            continue
        label = "群友" if role == "user" else "学姐"
        snippet = content.strip().replace("\n", " ")
        if len(snippet) > 120:
            snippet = snippet[:119] + "…"
        scored.append((sc, i, f"{label}：{snippet}"))

    scored.sort(key=lambda x: (-x[0], -x[1]))
    picked: list[str] = []
    seen: set[str] = set()
    for _, _, line in scored:
        if line in seen:
            continue
        seen.add(line)
        picked.append(line)
        if len(picked) >= recall_n:
            break
    if not picked:
        return recent, ""
    brief = "【相关旧聊天】（仅供回忆，别复读整段）\n" + "\n".join(f"- {p}" for p in picked)
    return recent, brief


def rank_person_items(
    items: list[str],
    query: str,
    *,
    keep: int,
) -> list[str]:
    """按当前句关键词重排 traits/moments，无命中则保持原序截断。"""
    if not items:
        return []
    keep = max(1, int(keep))
    keywords = extract_keywords(query)
    if not keywords:
        return items[:keep]
    scored = [(score_text(it, keywords), idx, it) for idx, it in enumerate(items)]
    if all(s <= 0 for s, _, _ in scored):
        return items[:keep]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [it for _, _, it in scored[:keep]]


def messages_for_llm_with_recall(
    history: list[dict[str, str]],
    query: str,
    *,
    recent_turns: int,
    recall_top: int,
    enable: bool,
) -> tuple[list[dict[str, Any]], str]:
    if not enable:
        return [{"role": m["role"], "content": m["content"]} for m in history], ""
    recent, brief = select_recent_and_recall(
        history,
        query,
        recent_turns=recent_turns,
        recall_top=recall_top,
    )
    return [{"role": m["role"], "content": m["content"]} for m in recent], brief
