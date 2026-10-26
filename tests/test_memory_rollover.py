"""会话历史的落盘滚动上限（2026-10-26）。

问题：max_history 只管「读给模型几轮」，磁盘文件仍然无限长——
实测有个私聊会话攒到 343 条。文件越长每轮 append 都要整份重写，
而且旧人设时期的回复原文会一直躺在里面被反复读到。

守三件事：
1. 超出上限的老消息挪进 .rollover 归档，不是删掉；
2. persist_max=0 时完全不裁（老行为可回退）；
3. 裁剪只影响磁盘，read 侧还要能正常拿到最近的历史。
"""

from __future__ import annotations

import json

from dl_senpai.memory import ChatMemory


def test_persist_limit_keeps_recent_and_archives_old(tmp_path):
    mem = ChatMemory(tmp_path / "memory", persist_max=10)
    for i in range(9):
        mem.append_turn("group:1", f"问{i}", f"答{i}")
    # 9 轮 = 18 条，超出 10 条上限
    kept = mem.load_all("group:1")
    assert len(kept) == 10
    assert kept[-1]["content"] == "答8"

    rollover = (tmp_path / "memory" / ".rollover" / "group_1.jsonl").read_text(encoding="utf-8")
    archived = [json.loads(line) for line in rollover.splitlines() if line.strip()]
    assert len(archived) == 8, "被滚出去的必须归档，不是丢掉"
    assert archived[0]["content"] == "问0"


def test_persist_limit_zero_is_unlimited(tmp_path):
    mem = ChatMemory(tmp_path / "memory", persist_max=0)
    for i in range(30):
        mem.append_turn("group:1", f"问{i}", f"答{i}")
    assert len(mem.load_all("group:1")) == 60, "0 = 不限，保持老行为"
    assert not (tmp_path / "memory" / ".rollover").exists()


def test_rollover_is_append_only_across_rounds(tmp_path):
    mem = ChatMemory(tmp_path / "memory", persist_max=4)
    for i in range(10):
        mem.append_turn("group:1", f"问{i}", f"答{i}")
    lines = [l for l in (tmp_path / "memory" / ".rollover" / "group_1.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(lines) == 16, "20 条写进来，留 4 条，其余 16 条都在归档里"
    assert len(mem.load_all("group:1")) == 4


def test_read_budget_also_applies(tmp_path):
    """字符预算只作用于读给模型那一份，不动磁盘。"""
    mem = ChatMemory(tmp_path / "memory", max_chars=20, persist_max=0)
    for i in range(6):
        mem.append_turn("group:1", "这是一句很长很长的用户发言" * 2, "这是一句很长很长的回复" * 2)
    assert len(mem.load("group:1")) == 2, "至少保住最后一问一答"
    assert len(mem.load_all("group:1")) == 12, "磁盘上一条都没少"

