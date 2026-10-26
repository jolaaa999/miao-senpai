"""按人记忆里的「没说出口的事」单测。

这一层是搬 ai-companion-kit 的 pending。它容易坏在三个地方：
写入时没进画像、读出来不进提示词、说完之后清不掉。
"""

from __future__ import annotations

from dl_senpai.person_memory import (
    PersonMemoryStore,
    PersonProfile,
    build_person_memory_brief,
    parse_impression_updates,
)


def test_pending_roundtrip(tmp_path):
    store = PersonMemoryStore(tmp_path)
    store.save(PersonProfile(user_id="10001", pending=["他还没回我那句话"]))

    profile = store.get("10001")
    assert profile.pending == ["他还没回我那句话"]
    # 只有 pending 也算「有内容」，否则这段心事永远不会被渲染进提示词
    assert profile.has_content() is True


def test_pending_rendered_in_brief():
    profile = PersonProfile(user_id="10001", pending=["他答应过要来找我"])
    brief = build_person_memory_brief(profile, name="老王")
    assert "没说出口的事" in brief
    assert "他答应过要来找我" in brief


def test_empty_pending_not_rendered():
    profile = PersonProfile(user_id="10001", traits=["常熬夜"])
    brief = build_person_memory_brief(profile, name="老王")
    assert "没说出口的事" not in brief


def test_impression_marker_writes_pending(tmp_path):
    store = PersonMemoryStore(tmp_path)
    raw = '<<<IMPRESSION {"pending": "他说下周要出差，但没说哪天"}>>>'
    parsed = parse_impression_updates(raw)
    assert parsed.updates and parsed.updates[0].pending == "他说下周要出差，但没说哪天"

    store.apply_updates("10001", parsed.updates)
    assert store.get("10001").pending == ["他说下周要出差，但没说哪天"]


def test_impression_marker_clears_pending(tmp_path):
    """说开了的事要能放下：空串 = 清空，否则心事只增不减。"""
    store = PersonMemoryStore(tmp_path)
    store.save(PersonProfile(user_id="10001", pending=["他还没回我那句"]))

    parsed = parse_impression_updates('<<<IMPRESSION {"pending": ""}>>>')
    store.apply_updates("10001", parsed.updates)
    assert store.get("10001").pending == []


def test_pending_deduplicates_and_caps(tmp_path):
    store = PersonMemoryStore(tmp_path, max_pending=2)
    store.apply_updates("1", parse_impression_updates('<<<IMPRESSION {"pending": "A"}>>>').updates)
    store.apply_updates("1", parse_impression_updates('<<<IMPRESSION {"pending": "A"}>>>').updates)
    assert store.get("1").pending == ["A"]

    store.apply_updates("1", parse_impression_updates('<<<IMPRESSION {"pending": "B"}>>>').updates)
    store.apply_updates("1", parse_impression_updates('<<<IMPRESSION {"pending": "C"}>>>').updates)
    assert store.get("1").pending == ["B", "C"]


def test_marker_with_only_pending_is_not_dropped():
    """旧解析器会把「只写 pending」的标记当成空更新丢掉，这里必须保住。"""
    parsed = parse_impression_updates('<<<IMPRESSION {"pending": "他今天没说话"}>>>')
    assert len(parsed.updates) == 1


def test_old_profile_without_pending_still_loads(tmp_path):
    """老画像文件里没有 pending 字段，读出来不能炸。"""
    store = PersonMemoryStore(tmp_path)
    (tmp_path / "user_20002.json").write_text(
        '{"user_id": "20002", "traits": ["爱玩太刀"], "notes": ""}', encoding="utf-8"
    )
    profile = store.get("20002")
    assert profile.pending == []
    assert profile.traits == ["爱玩太刀"]
