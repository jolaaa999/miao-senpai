"""心情滤镜：她现在什么心情，就更容易想起哪一类事（2026-10-26）。

思路来自小黑盒「大肥鱼教程篇2」的第五层心脏：心情不只是语气，
它还决定「你此刻能想起什么」。人闷的时候翻旧账，人高兴的时候
想起来的都是好笑的事——这不是算法，是人之常情。

三条约束，都是为了不把它做成假功能：
1. 只重排，不淘汰。命中就是命中，心情只决定先后；
   否则会出现「明明记过就是说想不起来」的假失忆。
2. 只做极性（难受 / 平常 / 高兴），不做加权平均。
   把「有点闷」和「有点慌」合成一个分数，等于什么都没说。
3. 认不出就 0。词表之外的心情词一律不干预排序，
   宁可没效果，也不要瞎猜出一个效果。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

# 难受：这一类词出现，说明这条记忆是「沉」的
_NEGATIVE_WORDS = (
    "闷", "烦", "难过", "委屈", "生气", "气死", "火大", "失望",
    "难受", "累", "倦", "疲惫", "冷落", "没人理", "忽略", "敷衍",
    "不安", "慌", "怕", "焦虑", "吃醋", "想哭", "孤单", "寂寞",
    "低落", "沉", "堵", "阴天",
)

# 高兴：这一类词出现，说明这条记忆是「亮」的
_POSITIVE_WORDS = (
    "开心", "高兴", "乐", "笑", "哈哈", "逗", "好玩", "有意思",
    "被夸", "夸", "喜欢", "暖", "踏实", "舒服", "轻松", "期待",
    "惊喜", "厉害", "棒", "甜", "阳光", "元气",
)


def _polarity_of(text: str) -> int:
    neg = sum(1 for w in _NEGATIVE_WORDS if w in text)
    pos = sum(1 for w in _POSITIVE_WORDS if w in text)
    if neg == pos:
        return 0
    return -1 if neg > pos else 1


def mood_polarity(word: Any) -> int:
    """把心情词收成 -1 / 0 / 1。认不出来就是 0（不干预）。"""
    text = str(word or "").strip()
    return _polarity_of(text) if text else 0


def text_polarity(text: Any) -> int:
    """一条文字的情绪极性。同样认不出就是 0。"""
    return _polarity_of(str(text or ""))


def rank_by_mood(
    items: Sequence[Any],
    mood_word: Any,
    *,
    text_of: Callable[[Any], str] | None = None,
    keep: int = 0,
) -> tuple[list[Any], int]:
    """按心情把候选重排，返回 (新列表, 位移数)。

    稳定排序：同极性优先，其余保持原顺序（原顺序通常已经是「新的在前」，
    这里不该把它搅乱，只做一次微调）。keep>0 时只返回前 keep 条。
    """
    rows = list(items)
    polarity = mood_polarity(mood_word)
    if not rows or polarity == 0:
        return (rows[:keep] if keep > 0 else rows), 0
    get_text = text_of or (lambda x: str(x))
    indexed = list(enumerate(rows))
    indexed.sort(
        key=lambda pair: (
            0 if text_polarity(get_text(pair[1])) == polarity else 1,
            pair[0],
        )
    )
    ranked = [item for _, item in indexed]
    # 位移数只用于观测：要确认「滤镜真的动过东西」，不能靠肉眼比对文案
    moved = sum(1 for pos, (idx, _) in enumerate(indexed) if pos != idx)
    return (ranked[:keep] if keep > 0 else ranked), moved


__all__ = ["mood_polarity", "rank_by_mood", "text_polarity"]
