from __future__ import annotations

from dl_senpai.memory_recall import (
    extract_keywords,
    messages_for_llm_with_recall,
    rank_person_items,
    select_recent_and_recall,
)


def test_extract_keywords() -> None:
    keys = extract_keywords("冰牙龙配装怎么弄 太刀")
    assert "冰牙龙" in keys or "配装" in keys
    assert "怎么" not in keys


def test_select_recent_and_recall() -> None:
    history: list[dict[str, str]] = []
    for i in range(40):
        history.append({"role": "user", "content": f"闲聊{i} 天气不错"})
        history.append({"role": "assistant", "content": f"嗯{i}"})
    history[2] = {"role": "user", "content": "冰牙龙弱点是什么"}
    history[3] = {"role": "assistant", "content": "头和翅膀比较软"}
    recent, brief = select_recent_and_recall(
        history,
        "还记得冰牙龙吗",
        recent_turns=10,
        recall_top=3,
    )
    assert len(recent) == 10
    assert "【相关旧聊天】" in brief
    assert "冰牙龙" in brief


def test_messages_for_llm_with_recall_disabled() -> None:
    hist = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "在"}]
    out, brief = messages_for_llm_with_recall(
        hist, "hi", recent_turns=2, recall_top=2, enable=False
    )
    assert len(out) == 2
    assert brief == ""


def test_rank_person_items() -> None:
    items = ["喜欢打太刀", "常问训练", "爱喝奶茶", "冰牙龙配装问过"]
    ranked = rank_person_items(items, "冰牙龙怎么打", keep=2)
    assert len(ranked) == 2
    assert ranked[0] == "冰牙龙配装问过"
