from __future__ import annotations

from pathlib import Path

from dl_senpai.memory import ChatMemory, session_id_for_group, session_id_for_private
from dl_senpai.session_fs import session_id_to_fs_key


def test_session_ids() -> None:
    assert session_id_for_group(123) == "group:123"
    assert session_id_for_private(9) == "private:9"


def test_append_and_trim() -> None:
    base = Path("data") / "_pytest_memory"
    base.mkdir(parents=True, exist_ok=True)
    mem_dir = base / "trim"
    if mem_dir.exists():
        for p in mem_dir.glob("*"):
            p.unlink()
    else:
        mem_dir.mkdir(parents=True, exist_ok=True)

    mem = ChatMemory(mem_dir, max_history=4)
    sid = "group:1"
    mem.append_turn(sid, "u1", "a1")
    mem.append_turn(sid, "u2", "a2")
    mem.append_turn(sid, "u3", "a3")
    history = mem.load(sid)
    assert len(history) == 4
    assert history[0]["content"] == "u2"
    assert history[-1]["content"] == "a3"


def test_clear() -> None:
    mem_dir = Path("data") / "_pytest_memory" / "clear"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem = ChatMemory(mem_dir, max_history=10)
    sid = "group:2"
    mem.append_turn(sid, "hi", "hello")
    mem.clear(sid)
    assert mem.load(sid) == []


def test_path_windows_safe() -> None:
    mem_dir = Path("data") / "_pytest_memory" / "safe"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem = ChatMemory(mem_dir, max_history=4)
    assert mem._path("group:123").name == "group_123.json"
    assert mem._path("private:9").name == "private_9.json"
    assert session_id_to_fs_key("group:123") == "group_123"


def test_corrupt_file() -> None:
    mem_dir = Path("data") / "_pytest_memory" / "corrupt"
    mem_dir.mkdir(parents=True, exist_ok=True)
    mem = ChatMemory(mem_dir, max_history=10)
    sid = "group:bad"
    path = mem._path(sid)
    path.write_text("{not json", encoding="utf-8")
    assert mem.load(sid) == []
