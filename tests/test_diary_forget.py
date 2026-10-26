"""日记的遗忘机制（2026-10-25 加，思路来自小黑盒「大肥鱼优化篇2」）。

要守住的四条：
1. 寿命是**绝对时长**，从写下那天算，不跟「最后一次想起」走；
2. 翻看不续命，只有明确延长才加天数；
3. 到期先进回收站（软删除），不是直接抹掉；
4. 注入只给模糊视图，剩余越少给得越糊——全文回放会把旧人设的措辞
   一遍遍喂回给模型（线上症状：记忆「又记得又混乱」）。
"""

from __future__ import annotations

import time

from dl_senpai.diary import (
    TIER_IMPORTANT,
    TIER_NORMAL,
    parse_diary_reply,
    TIER_PARKED,
    TIER_UNFORGETTABLE,
    DiaryEntry,
    DiaryStore,
    build_diary_brief,
)

DAY = 86400.0


def _store(tmp_path) -> DiaryStore:
    return DiaryStore(tmp_path / "diary")


def test_default_life_is_seven_days_and_expires_into_recycle_bin(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.add(DiaryEntry(text="九天前随口聊的天气。", created_at=now - 9 * DAY))
    # 排期是惰性的：读注入时结算（build_diary_brief 会调它）
    assert [e.text for e in store.expire()]
    assert store.entries() == [], "过期的应当已经不再可见"
    # 但数据还在：回收站里能翻出来（划掉 ≠ 抹掉）
    recycled = store.entries(include_deleted=True)
    assert len(recycled) == 1 and recycled[0].is_deleted


def test_extend_accumulates_and_only_explicit_calls_count(tmp_path):
    store = _store(tmp_path)
    store.add(DiaryEntry(text="他问德育线怎么算。", created_at=time.time()))
    eid = store.entries()[0].entry_id
    assert store.find(eid).total_life_days == 7
    store.extend(eid)
    assert store.find(eid).total_life_days == 10, "普通档延长一次 +3"
    store.extend(eid)
    assert store.find(eid).total_life_days == 13, "延长是在当前寿命上累加"


def test_reading_does_not_extend_life(tmp_path):
    """翻看不续命：反复渲染注入，寿命不能变。"""
    store = _store(tmp_path)
    store.add(DiaryEntry(text="他问我推荐信怎么写。", created_at=time.time()))
    eid = store.entries()[0].entry_id
    for _ in range(5):
        store.context(5)
    assert store.find(eid).total_life_days == 7


def test_upgrade_resets_to_new_tier_life(tmp_path):
    store = _store(tmp_path)
    store.add(DiaryEntry(text="他说他失恋过。", created_at=time.time()))
    eid = store.entries()[0].entry_id
    store.extend(eid)                      # 7 -> 10
    store.set_tier(eid, TIER_UNFORGETTABLE)
    # 按新档位重算，不在旧寿命上叠加
    assert store.find(eid).total_life_days == 90


def test_parked_entries_never_decay(tmp_path):
    store = _store(tmp_path)
    store.add(DiaryEntry(text="不想看的那件事。", created_at=time.time() - 100 * DAY))
    eid = store.entries()[0].entry_id
    store.park(eid)
    parked = store.find(eid)
    assert parked.tier == TIER_PARKED
    assert not parked.is_deleted, "搁着不衰减，一百天前写的也还在"
    assert parked.days_left() > 1000
    # 拿出来以后按新档位重新算寿命
    store.unpark(eid, TIER_IMPORTANT)
    assert store.find(eid).total_life_days == 21


def test_remove_and_restore(tmp_path):
    store = _store(tmp_path)
    store.add(DiaryEntry(text="随手记的一句。", created_at=time.time()))
    eid = store.entries()[0].entry_id
    store.remove(eid)
    assert store.entries() == []
    store.restore(eid)
    assert len(store.entries()) == 1


def test_injection_is_blurred_and_degrades_with_life(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.add(
        DiaryEntry(
            text="他问我德育线怎么算，我讲了半天，他说还是没懂，我说你自己查官网。",
            created_at=now - 600,
            mood="还行",
        )
    )
    brief = store.context(3)
    assert "他自己查官网" not in brief, "不该把正文整段塞进去"
    assert "…" in brief, "只给开头一小段 + 省略号"
    assert "id=" in brief, "给 id 才可能让她按 id 展开"

    # 寿命过半以后只剩日期，细节一概不给
    store2 = _store(tmp_path / "b")
    store2.add(DiaryEntry(text="快忘干净的东西。", created_at=now - 6.5 * DAY, mood="闷"))
    brief2 = store2.context(3)
    assert "快忘干净" not in brief2
    assert "闷" not in brief2, "剩余不到两成时连心情都不给"


def test_due_soon_warns_once(tmp_path):
    store = _store(tmp_path)
    store.add(DiaryEntry(text="六天前的事，快到期了。", created_at=time.time() - 6 * DAY))
    first = build_diary_brief(store, n=3)
    assert "【日记快到期了】" in first
    assert "还剩 1 天" in first
    second = build_diary_brief(store, n=3)
    assert "【日记快到期了】" not in second, "同一档位一天只提醒一次"


def test_expired_entry_disappears_from_injection(tmp_path):
    store = _store(tmp_path)
    store.add(DiaryEntry(text="八天前随口聊的。", created_at=time.time() - 8 * DAY))
    brief = build_diary_brief(store, n=3)
    assert brief == "" or "随口聊的" not in brief


def test_model_can_tag_tier_when_writing():
    """档位由她自己标；没标就按普通算（7 天）。"""
    # 注意：parse_diary_reply 吃的是模型原文（JSON 字符串），不是 dict
    marked = parse_diary_reply('{"text": "他说他失恋过，我记住了。", "tier": "重要"}')
    assert marked["tier"] == TIER_IMPORTANT
    plain = parse_diary_reply('{"text": "随口聊了两句天气。"}')
    assert plain["tier"] == TIER_NORMAL
    # 模型乱写档位名也不能炸，收成普通
    weird = parse_diary_reply('{"text": "随便。", "tier": "特别特别重要"}')
    assert weird["tier"] == TIER_NORMAL


def test_tier_changes_lifetime_on_write(tmp_path):
    store = _store(tmp_path)
    store.add(parse_diary_reply('{"text": "他说他失恋过。", "tier": "重要"}'))
    entry = store.entries()[0]
    assert entry.tier == TIER_IMPORTANT
    assert entry.total_life_days == 21


def test_day_file_shows_id_and_tier(tmp_path):
    """md 副本要能人工核对：编号对着回收站，档位看着知道为什么还在。"""
    store = _store(tmp_path)
    store.add(DiaryEntry(text="他问我德育线怎么算。", title="德育线", mood="还行"))
    store.add(DiaryEntry(text="他说他失恋过。", title="旧事", tier=TIER_IMPORTANT))
    day = store.entries()[0].day
    body = store.day_path(day).read_text(encoding="utf-8")
    ids = [e.entry_id for e in store.entries()]
    for eid in ids:
        assert f"id={eid}" in body
    assert "〔重要〕" in body
    assert "〔普通〕" not in body, "普通是默认档，不写进 md，免得满屏噪音"
