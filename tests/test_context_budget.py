"""上下文预算：条数和字符两道闸。"""

from __future__ import annotations

from dl_senpai.memory import ChatMemory
from dl_senpai.memory_recall import messages_for_llm_with_recall, trim_by_char_budget


def _msgs(n: int, size: int) -> list[dict[str, str]]:
    out = []
    for i in range(n):
        out.append({"role": "user" if i % 2 == 0 else "assistant", "content": "啊" * size})
    return out


def test_zero_budget_keeps_everything():
    msgs = _msgs(10, 100)
    assert trim_by_char_budget(msgs, 0) == msgs


def test_budget_keeps_newest():
    # 每条 100 字，预算 250 → 只留最近 2 条
    msgs = _msgs(10, 100)
    out = trim_by_char_budget(msgs, 250)
    assert out == msgs[-2:]
    assert out[-1] is msgs[-1], "必须保留最新那条"


def test_min_keep_survives_one_huge_message():
    """单条就超预算时也不能全砍——至少留最近几条，否则她像失忆。"""
    msgs = [{"role": "user", "content": "长" * 50000}]
    out = trim_by_char_budget(msgs, 100)
    assert len(out) == 1


def test_min_keep_keeps_last_turn_only():
    """预算远小于单条时，只无条件保留最后一轮（一问一答）。"""
    msgs = _msgs(20, 1000)
    out = trim_by_char_budget(msgs, 100)
    assert len(out) == 2


def test_empty_history_safe():
    assert trim_by_char_budget([], 100) == []


def test_messages_for_llm_respects_budget_with_recall_off():
    msgs = _msgs(30, 100)
    out, brief = messages_for_llm_with_recall(
        msgs, "有没有讲 PPT 的事", recent_turns=24, recall_top=4, enable=False, max_chars=300
    )
    assert brief == ""
    assert sum(len(m["content"]) for m in out) <= 300


def test_memory_load_applies_char_budget(tmp_path):
    """ChatMemory.load() 也套预算（直接读历史的路径）。"""
    mem = ChatMemory(tmp_path, max_history=0, max_chars=250)
    mem.save("group:1", _msgs(20, 100))
    loaded = mem.load("group:1")
    assert len(loaded) < 20
    assert sum(len(m["content"]) for m in loaded) <= 250 + 2 * 100


def test_memory_append_turn_does_not_shrink_disk(tmp_path):
    """预算只作用于「读给模型」，绝不能在写回时删掉用户历史。"""
    mem = ChatMemory(tmp_path, max_history=0, max_chars=100)
    mem.save("group:1", _msgs(10, 100))
    mem.append_turn("group:1", "问一句", "答一句")
    on_disk = mem.load_all("group:1")
    assert len(on_disk) == 12, f"磁盘历史被预算删了：{len(on_disk)}"
