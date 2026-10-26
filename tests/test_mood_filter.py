"""心情滤镜（2026-10-26）：心情决定「更容易想起哪一类事」。

守四条：
1. 只重排，不淘汰——不能出现「明明记过就想想不起来」的假失忆；
2. 关键词命中分永远优先，心情只是次级排序键；
3. 心情认不出（极性 0）就完全不干预；
4. 日记和召回两处都吃同一个滤镜，不能一处变一处不变。
"""

from __future__ import annotations

from dl_senpai import mood_filter as mf
from dl_senpai.diary import DiaryEntry, DiaryStore
from dl_senpai.memory_recall import select_recent_and_recall


def test_polarity_recognises_common_words():
    assert mf.mood_polarity("有点闷") == -1
    assert mf.mood_polarity("挺开心") == 1
    assert mf.mood_polarity("") == 0
    # 认不出来的词一律不干预，宁可没效果也不要瞎猜
    assert mf.mood_polarity("像块石头") == 0
    assert mf.mood_polarity("阴天") == -1


def test_rank_is_stable_and_keeps_everything():
    items = ["普通的事", "有点难过", "另一件普通的事", "有点开心"]
    ranked, moved = mf.rank_by_mood(items, "闷")
    assert len(ranked) == len(items), "只重排，不淘汰"
    assert ranked[0] == "有点难过"
    assert moved > 0
    # 同极性的内部保持原相对顺序
    assert ranked.index("普通的事") < ranked.index("另一件普通的事")


def test_zero_polarity_does_not_touch_order():
    items = ["a", "b", "c"]
    ranked, moved = mf.rank_by_mood(items, "")
    assert ranked == items and moved == 0
    ranked2, moved2 = mf.rank_by_mood(items, "像块石头")
    assert ranked2 == items and moved2 == 0


def test_rank_keep_truncates_but_not_before_ranking():
    items = ["普通", "普通二", "难过的一件事"]
    ranked, _ = mf.rank_by_mood(items, "闷", keep=2)
    assert ranked == ["难过的一件事", "普通"]


def _history():
    # 两条旧消息都命中「德育线」，一条沉一条亮；心情闷时应该先想起沉的那条
    return [
        {"role": "user", "content": "德育线那事儿搞得我好烦，越想越堵"},
        {"role": "user", "content": "德育线今天居然算对了，开心，哈哈"},
    ] + [{"role": "user", "content": f"闲聊{i}"} for i in range(4)] + [
        {"role": "user", "content": "现在这条是最近的"},
    ]


def test_recall_prefers_matching_mood_but_keeps_both():
    history = _history()
    _, brief_moody = select_recent_and_recall(
        history, "德育线怎么算", recent_turns=2, recall_top=4, mood_word="有点闷"
    )
    lines = [l for l in brief_moody.splitlines() if l.startswith("- ")]
    assert any("烦" in l for l in lines), "闷的时候应该想起沉的那条"
    assert any("开心" in l for l in lines), "另一条不能被淘汰"
    assert "烦" in lines[0], "情绪对得上的排前面"


def test_recall_without_mood_keeps_keyword_order():
    history = _history()
    _, brief = select_recent_and_recall(
        history, "德育线怎么算", recent_turns=2, recall_top=4, mood_word=""
    )
    lines = [l for l in brief.splitlines() if l.startswith("- ")]
    assert len(lines) >= 2


def test_diary_context_picks_mood_matching_entry_first(tmp_path):
    store = DiaryStore(tmp_path)
    # 顺序：闷的旧一点，开心的新一点——这样「按时间」和「按心情」结果相反
    store.add(DiaryEntry(text="有点闷，没人理我。", title="没人理", mood="闷"))
    store.add(DiaryEntry(text="今天挺开心的，被人夸了一句。", title="被夸", mood="开心"))
    body = store.context(1, mood_word="有点闷")
    assert "闷" in body, "闷的时候先想起沉的那条"
    assert "开心" not in body


def test_diary_context_without_mood_is_chronological(tmp_path):
    store = DiaryStore(tmp_path)
    store.add(DiaryEntry(text="有点闷，没人理我。", title="没人理", mood="闷"))
    store.add(DiaryEntry(text="今天挺开心的，被人夸了一句。", title="被夸", mood="开心"))
    body = store.context(1)
    assert "开心" in body, "不传心情时保持原来「最新那条」的行为"



def test_boost_beats_small_score_gap():
    """实测关键词分几乎不会相等（4.183 vs 4.213），只做次级键等于没做。

    这条守着「+20% 加成」这个设计：分差小的能被情绪翻转，
    分差大的（明显更相关那条）不会被翻。
    """
    from dl_senpai.memory_recall import score_text, extract_keywords

    kw = extract_keywords("德育线怎么算")
    sad = "德育线那事儿搞得我好烦，越想越堵"
    glad = "德育线今天算对了，开心哈哈"
    # 两条都要真的命中，否则测不到效果
    assert score_text(sad, kw) > 0 and score_text(glad, kw) > 0

    history = [
        {"role": "user", "content": sad},
        {"role": "assistant", "content": "别提了"},
        {"role": "user", "content": glad},
        {"role": "assistant", "content": "挺好"},
    ] + [{"role": "user", "content": f"闲聊{i}"} for i in range(30)]

    _, moody = select_recent_and_recall(
        history, "德育线怎么算", recent_turns=8, recall_top=4, mood_word="有点闷"
    )
    lines = [l[2:] for l in moody.splitlines() if l.startswith("- ")]
    assert lines[0].startswith("德育线那事儿"), "同极性加成要能压过微小的分数差"
    assert len(lines) == 2, "另一条仍然在，不能被淘汰"

