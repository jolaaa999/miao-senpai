"""跨场合对话流：她换个地方还认得你。

这是从 ai-companion 包里搬来的唯一一个我们没做过的东西。
它自己的说法：决定一个陪聊像不像人的不是模型，是「记不记得你」。
它指出的病根不是记忆太短，是**账本分错了维度**——按会话存，
同一个人换到群里就失忆。

这里重点测的不是「能存能读」，是**隐私边界**：
私聊的内容绝不允许渲进群里的提示词。
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from dl_senpai.cross_chat import CrossChatStore, build_cross_brief


def _store(tmp: str) -> CrossChatStore:
    return CrossChatStore(tmp)


def test_records_and_renders():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        st.record(where="group:1", who="小明", text="今晚打本吗", reply="打呀")
        brief = build_cross_brief(current_where="private:9", directory=tmp)
        assert "今晚打本吗" in brief
        assert "群里" in brief


def test_same_session_not_repeated():
    """本会话的内容不该再注入一遍：ChatMemory 已经喂过了。"""
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        st.record(where="group:1", who="小明", text="今晚打本吗", reply="打呀")
        assert build_cross_brief(current_where="group:1", directory=tmp) == ""


def test_private_never_leaks_into_group():
    """这条是硬规则：私聊内容不许出现在群里的提示词里。"""
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        st.record(where="private:9", who="小明", text="我考试挂了别告诉别人", reply="嗯")
        assert build_cross_brief(current_where="group:1", directory=tmp) == ""


def test_private_visible_in_private():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        st.record(where="private:9", who="小明", text="在忙吗", reply="在")
        brief = build_cross_brief(current_where="private:10", directory=tmp)
        assert "在忙吗" in brief


def test_group_visible_from_other_group():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        st.record(where="group:1", who="小明", text="群里的话", reply="嗯")
        brief = build_cross_brief(current_where="group:2", directory=tmp)
        assert "群里的话" in brief


def test_recent_n_and_char_budget():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        for i in range(20):
            st.record(where="group:1", who="甲", text=f"第{i}条" * 20, reply="嗯")
        brief = build_cross_brief(current_where="group:2", directory=tmp, n=3, max_chars=120)
        assert len(brief) <= 120


def test_empty_store_returns_empty():
    with tempfile.TemporaryDirectory() as tmp:
        assert build_cross_brief(current_where="group:1", directory=tmp) == ""


def test_broken_lines_are_skipped():
    with tempfile.TemporaryDirectory() as tmp:
        st = _store(tmp)
        st.record(where="group:1", who="甲", text="好的", reply="嗯")
        path = Path(tmp) / "turns.jsonl"
        path.write_text(path.read_text(encoding="utf-8") + "{不是 json}\n", encoding="utf-8")
        brief = build_cross_brief(current_where="group:2", directory=tmp)
        assert "好的" in brief
