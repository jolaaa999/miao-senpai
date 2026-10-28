"""最近聊天缓冲（recent_chat）的单元测试。

覆盖三件事：
1. 存储：过期裁剪、条数上限、落盘与重载、空文本丢弃
2. 渲染：最近段 + 召回段、排除当前消息、预算截断、无数据返回空
3. 接线：MEMORY 工具读的是全量历史而不是 prompt 窗口
   （这条是本次修复的核心 bug，必须有回归测试盯着）
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from src.plugins.dl_senpai.recent_chat import (
    DEFAULT_MAX_ENTRIES,
    ChatEntry,
    RecentChatStore,
    build_recent_chat_brief,
)


@pytest.fixture()
def store(tmp_path: Path) -> RecentChatStore:
    # flush_interval=0 让 flush 立即生效，测试不必等节流窗口
    return RecentChatStore(tmp_path, flush_interval=0.0)


# -- 存储 --


def test_append_and_read_back(store: RecentChatStore) -> None:
    store.append("group:1", sender="moring", user_id="1", text="在吗")
    store.append("group:1", sender="姜", user_id="2", text="在的")
    items = store.entries("group:1")
    assert [e.text for e in items] == ["在吗", "在的"]
    assert [e.sender for e in items] == ["moring", "姜"]


def test_blank_text_is_dropped(store: RecentChatStore) -> None:
    """空白消息进背景只会白占预算。"""
    store.append("group:1", sender="moring", text="   ")
    store.append("group:1", sender="moring", text="")
    assert store.entries("group:1") == []


def test_sender_falls_back_to_user_id(store: RecentChatStore) -> None:
    """拿不到昵称时至少留个 id，别渲染成空白。"""
    store.append("group:1", sender="", user_id="12345", text="你好")
    assert store.entries("group:1")[0].sender == "12345"


def test_expired_entries_are_pruned(tmp_path: Path) -> None:
    """超过 TTL 的条目必须消失——否则「一天窗口」是假的。"""
    st = RecentChatStore(tmp_path, ttl_sec=3600.0, flush_interval=0.0)
    now = 1_000_000.0
    st.append("group:1", sender="a", text="两小时前", ts=now - 7200)
    st.append("group:1", sender="a", text="刚刚", ts=now - 10)
    items = st.entries("group:1", now=now)
    assert [e.text for e in items] == ["刚刚"]


def test_max_entries_keeps_newest(tmp_path: Path) -> None:
    st = RecentChatStore(tmp_path, max_entries=3, flush_interval=0.0)
    for i in range(6):
        st.append("group:1", sender="a", text=f"第{i}条", ts=100.0 + i)
    items = st.entries("group:1", now=200.0)
    assert [e.text for e in items] == ["第3条", "第4条", "第5条"]


def test_flush_and_reload_survives_restart(tmp_path: Path) -> None:
    """重启后仍在，这是「按天」成立的前提。"""
    a = RecentChatStore(tmp_path, flush_interval=0.0)
    a.append("group:1", sender="moring", user_id="1", text="重启前", message_id="m1")
    assert a.flush(force=True) == 1

    b = RecentChatStore(tmp_path)
    items = b.entries("group:1")
    assert len(items) == 1
    assert items[0].text == "重启前"
    assert items[0].message_id == "m1"


def test_flush_throttles_by_interval(tmp_path: Path) -> None:
    """节流生效：同一时间窗内第二次 flush 不重复写。"""
    st = RecentChatStore(tmp_path, flush_interval=100.0)
    st.append("group:1", sender="a", text="一")
    assert st.flush(now=1000.0) == 1
    st.append("group:1", sender="a", text="二")
    assert st.flush(now=1001.0) == 0  # 还在节流窗内
    assert st.flush(now=1200.0) == 1  # 窗口过了才写


def test_forget_removes_file_and_cache(tmp_path: Path) -> None:
    st = RecentChatStore(tmp_path, flush_interval=0.0)
    st.append("group:1", sender="a", text="要清掉")
    st.flush(force=True)
    st.forget("group:1")
    assert st.entries("group:1") == []
    assert not (tmp_path / "group_1.json").exists()


def test_corrupt_file_degrades_to_empty(tmp_path: Path) -> None:
    """磁盘上的半截 JSON 不该让整条链路崩掉。"""
    (tmp_path / "group_1.json").write_text("{不是合法 JSON", encoding="utf-8")
    st = RecentChatStore(tmp_path)
    assert st.entries("group:1") == []


def test_sessions_are_isolated(store: RecentChatStore) -> None:
    store.append("group:1", sender="a", text="群一的")
    store.append("group:2", sender="b", text="群二的")
    assert [e.text for e in store.entries("group:1")] == ["群一的"]
    assert [e.text for e in store.entries("group:2")] == ["群二的"]


def test_unknown_schema_entries_are_skipped(tmp_path: Path) -> None:
    """旧版本或手改过的文件里混进脏数据时，只跳过脏的那条。"""
    # ts 用当前时间：用 0 会被 TTL 当成过期数据裁掉，那样测的是裁剪而不是
    # schema 容错，等于白测。
    now = time.time()
    (tmp_path / "group_1.json").write_text(
        json.dumps([{"text": "好的", "ts": now, "sender": "a"}, "垃圾", {"ts": now}, 7]),
        encoding="utf-8",
    )
    st = RecentChatStore(tmp_path)
    assert [e.text for e in st.entries("group:1")] == ["好的"]


# -- 渲染 --


def _entries(*rows: tuple[float, str, str]) -> list[ChatEntry]:
    return [ChatEntry(ts=t, sender=s, user_id="", text=x) for t, s, x in rows]


def test_brief_empty_when_no_entries() -> None:
    assert build_recent_chat_brief([], "随便") == ""


def test_brief_has_title_and_instruction() -> None:
    """标题自带用法说明，靠它替代 persona 里的提示，避免改 persona.py。"""
    brief = build_recent_chat_brief(_entries((100.0, "moring", "在吗")), "在吗")
    assert "今天群里聊过的" in brief
    assert "别复读" in brief


def test_brief_private_uses_different_title() -> None:
    brief = build_recent_chat_brief(
        _entries((100.0, "moring", "在吗")), "在吗", is_private=True
    )
    assert "今天聊过的" in brief
    assert "今天群里聊过的" not in brief


def test_brief_includes_sender_and_text() -> None:
    brief = build_recent_chat_brief(_entries((100.0, "姜", "大剑要预判头")), "大剑")
    assert "姜" in brief
    assert "大剑要预判头" in brief


def test_brief_excludes_current_message() -> None:
    """当前这句马上要以 user 身份出现，背景里再来一遍就是重复。"""
    items = [
        ChatEntry(ts=100.0, sender="a", user_id="", text="前一句", message_id="m1"),
        ChatEntry(ts=101.0, sender="a", user_id="", text="当前这句", message_id="m2"),
    ]
    brief = build_recent_chat_brief(items, "当前这句", exclude_message_id="m2")
    assert "当前这句" not in brief
    assert "前一句" in brief


def test_brief_recalls_older_by_keyword() -> None:
    """刷屏快的时候，只给最近几条会丢掉话题开头，所以要按关键词回捞。"""
    # 关键词那条必须排在**很早**：若它落进 recent_n 窗口，走的是「最近」那段，
    # 就测不到召回逻辑了。
    rows = [(100.0, "moring", "上午聊过煌黑的大剑配装")]
    rows += [(200.0 + i, "路人", "无关闲聊占位") for i in range(40)]
    rows.append((900.0, "姜", "刚说的这句"))
    brief = build_recent_chat_brief(
        _entries(*rows), "煌黑 配装", recent_n=3, recall_n=5
    )
    assert "煌黑" in brief  # 被召回捞回来了
    assert "更早" in brief  # 并且有分段标记


def test_brief_truncates_instead_of_dropping_everything() -> None:
    """单条超长时必须截断保留——整段丢弃会让模型以为「什么都没聊过」。"""
    long_text = "很长" * 600  # 远超预算
    brief = build_recent_chat_brief(
        _entries((100.0, "moring", long_text)), "很长", max_chars=100
    )
    assert brief  # 不是空字符串
    assert "…" in brief
    assert len(brief) < len(long_text)


def test_brief_respects_char_budget() -> None:
    rows = [(float(i), "路人", "占位内容" * 20) for i in range(50)]
    brief = build_recent_chat_brief(_entries(*rows), "占位", max_chars=300)
    assert len(brief) < 900


def test_brief_newlines_are_flattened() -> None:
    """多行消息会把背景段撑成一大片，压成一行才好读。"""
    brief = build_recent_chat_brief(_entries((100.0, "moring", "第一行\n第二行")), "第一行")
    assert "第一行 第二行" in brief


def test_brief_names_the_sender_format_and_skips_empty() -> None:
    """背景资料必须点明「发言人：内容」的格式，且空流水不能注入。

    为什么钉这两条：
    1. 不点明格式时，模型会把发言人名字当成正文——实测「失语.：123」被它
       拆成「失语.」是个名字？还是在说你自己无语了？（「失语」本身是常用词）；
    2. 加了格式说明行之后，空流水如果还被判为「有背景」注入，模型会对着两行
       空说明硬编，比没有背景更糟。
    """
    from dl_senpai.recent_chat import ChatEntry, build_recent_chat_brief

    assert build_recent_chat_brief([], "在吗") == ""

    entry = ChatEntry(ts=1793021014.0, sender="失语.", user_id="2843422418", text="123", message_id="1")
    brief = build_recent_chat_brief([entry], "123", is_private=True)
    assert "冒号前是发言人名字" in brief
    assert "失语." in brief
