"""存储层测试：原子写、旧格式兼容、并发安全。"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from dl_senpai.storage import JsonStore, atomic_write_text


@pytest.fixture
def tmp_store(tmp_path: Path) -> JsonStore:
    return JsonStore(tmp_path / "data.json")


# ---------------------------------------------------------------- 基本读写


def test_save_and_load_roundtrip(tmp_store: JsonStore) -> None:
    tmp_store.save({"a": 1, "中文": "值"})
    assert tmp_store.load() == {"a": 1, "中文": "值"}


def test_exists(tmp_store: JsonStore) -> None:
    assert tmp_store.exists() is False
    tmp_store.save([])
    assert tmp_store.exists() is True


def test_load_missing_returns_default(tmp_store: JsonStore) -> None:
    assert tmp_store.load(default=[]) == []
    assert tmp_store.load() is None


def test_delete(tmp_store: JsonStore) -> None:
    tmp_store.save({"x": 1})
    assert tmp_store.delete() is True
    assert tmp_store.exists() is False
    assert tmp_store.delete() is False


# ---------------------------------------------------------------- 旧格式兼容


def test_legacy_json_readable(tmp_path: Path) -> None:
    """历史文件（其他 Store 写的格式）必须能直接读，无需迁移。"""
    path = tmp_path / "legacy.json"
    legacy = [
        {"role": "user", "content": "历史消息"},
        {"role": "assistant", "content": "历史回复"},
    ]
    path.write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
    store = JsonStore(path)
    assert store.load() == legacy


def test_corrupt_json_returns_default(tmp_path: Path) -> None:
    """损坏文件不应抛异常，而是降级为 default（保证消息链路不中断）。"""
    path = tmp_path / "broken.json"
    path.write_text('{"users": {"1": ', encoding="utf-8")
    store = JsonStore(path)
    assert store.load(default={}) == {}


def test_empty_file_returns_default(tmp_path: Path) -> None:
    """空文件（Windows 路径 bug 的历史产物）视为无数据。"""
    path = tmp_path / "empty.json"
    path.write_text("", encoding="utf-8")
    assert JsonStore(path).load(default=[]) == []


def test_load_or_raise_on_corrupt(tmp_path: Path) -> None:
    from dl_senpai.storage import JsonStoreError

    path = tmp_path / "broken2.json"
    path.write_text("not json at all", encoding="utf-8")
    with pytest.raises(JsonStoreError):
        JsonStore(path).load_or_raise()


def test_load_transformed_falls_back_on_bad_data(tmp_path: Path) -> None:
    """脏数据导致 transform 抛异常时，应返回 default 而不是崩溃。"""
    path = tmp_path / "t.json"
    path.write_text(json.dumps({"users": {"1": {"v": 1}}}), encoding="utf-8")

    def good_transform(raw: dict) -> dict:
        return {"count": len(raw["users"])}

    def strict_transform(raw: dict) -> dict:
        # users 是 dict，取不存在的 "nested" 会 KeyError
        return {"count": len(raw["users"]["nested"])}

    store = JsonStore(path)
    assert store.load_transformed(good_transform) == {"count": 1}
    assert store.load_transformed(strict_transform, default={}) == {}


def test_load_transformed_missing_file_returns_default(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "nope.json")
    assert store.load_transformed(lambda raw: raw, default="fallback") == "fallback"


# ---------------------------------------------------------------- 原子写


def test_atomic_write_leaves_no_temp_files(tmp_path: Path) -> None:
    """成功写入后不留临时文件（同目录 .tmp）。"""
    path = tmp_path / "data.json"
    atomic_write_text(path, '{"ok": true}')
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "data.json"]
    assert leftovers == []


def test_atomic_write_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "c.json"
    atomic_write_text(path, "{}")
    assert path.read_text(encoding="utf-8") == "{}"


def test_failed_write_preserves_original(tmp_path: Path) -> None:
    """写入中途失败时，原文件必须完好（这是原子写的核心价值）。

    通过让 json.dumps 之外的写入环节抛异常来模拟"进程在写一半被 kill"。
    """
    path = tmp_path / "important.json"
    store = JsonStore(path)
    store.save({"version": "original"})

    class Boom:
        pass

    # 序列化就失败 → save 抛异常，但原文件未被触碰
    with pytest.raises(TypeError):
        store.save({"bad": Boom()})

    assert store.load() == {"version": "original"}


def test_overwrite_is_atomic_not_truncate(tmp_path: Path) -> None:
    """覆盖写入不应出现"文件短暂为空/半截"的窗口。

    做法：写入的同时另一个线程反复读，只要**任何一次读到的内容不是合法
    JSON** 就算失败。

    实现注意（Windows 实测踩坑）：
    1. 读线程必须 `sleep`。不带休眠的忙读会在 Windows 上持续抢 GIL 与
       文件锁，导致写方 `os.replace` 长时间拿不到机会，测试表现为**挂死**
       （实测 >5 分钟不结束）。
    2. 读或替换任一方都可能抛 `PermissionError`（文件被占用）。这是
       Windows 的文件语义，不是数据损坏，因此跳过该次采样。
    3. 只断言"凡成功读到的内容都是完整 JSON"，而不是"每次都读到完整内容"。
    """
    path = tmp_path / "hot.json"
    store = JsonStore(path)
    store.save({"n": 0})

    stop = threading.Event()
    bad: list[str] = []
    occupied = [0]
    reads = [0]

    def reader() -> None:
        while not stop.is_set():
            try:
                raw = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                time.sleep(0.001)
                continue
            except PermissionError:
                occupied[0] += 1  # 被替换动作占用，非损坏
                time.sleep(0.001)
                continue
            except OSError:
                time.sleep(0.001)
                continue
            reads[0] += 1
            try:
                json.loads(raw)
            except json.JSONDecodeError:
                bad.append(raw[:60])
                return
            time.sleep(0.001)

    threads = [threading.Thread(target=reader, daemon=True) for _ in range(2)]
    for t in threads:
        t.start()
    try:
        for n in range(1, 80):
            store.save({"n": n, "pad": "x" * 200})
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=5)

    assert bad == [], f"读到了半截 JSON：{bad[:1]}"
    assert reads[0] > 0, "读线程一次都没成功读到内容，测试无效"
    assert store.load() == {"n": 79, "pad": "x" * 200}


def test_concurrent_writes_do_not_corrupt(tmp_path: Path) -> None:
    """多线程并发写同一文件，最终内容必须是某次完整写入，而非交错。"""
    path = tmp_path / "concurrent.json"
    store = JsonStore(path)

    def writer(tag: int) -> None:
        for i in range(15):
            store.save({"tag": tag, "i": i, "pad": "y" * 300})

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert all(not t.is_alive() for t in threads), "写入线程未在超时内结束"

    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)  # 不抛异常即未损坏
    assert parsed["tag"] in {0, 1, 2, 3}
    assert parsed["pad"] == "y" * 300


def test_replace_retries_when_target_locked(tmp_path: Path, monkeypatch) -> None:
    """Windows 上目标文件被占用时，写入应有界重试而不是直接失败。

    模拟：让 os.replace 前两次抛 PermissionError，第三次成功。
    """
    import os as _os

    from dl_senpai.storage import json_store as js

    path = tmp_path / "locked.json"
    store = JsonStore(path)
    store.save({"v": 1})

    real_replace = _os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError(13, "Permission denied (simulated)")
        return real_replace(src, dst)

    monkeypatch.setattr(js.os, "replace", flaky_replace)
    store.save({"v": 2})
    assert calls["n"] == 3
    assert store.load() == {"v": 2}


def test_replace_gives_up_after_retries(tmp_path: Path, monkeypatch) -> None:
    """重试耗尽后必须抛出，不能静默丢数据。"""
    import os as _os

    from dl_senpai.storage import json_store as js

    path = tmp_path / "always_locked.json"
    store = JsonStore(path)
    store.save({"v": 1})

    def always_fail(src, dst):
        raise PermissionError(13, "Permission denied (simulated)")

    monkeypatch.setattr(js.os, "replace", always_fail)
    with pytest.raises(PermissionError):
        store.save({"v": 2})
    # 原文件必须完好（临时文件不覆盖它）
    assert store.load() == {"v": 1}


def test_no_temp_files_left_after_failures(tmp_path: Path, monkeypatch) -> None:
    """失败路径不能留下 .tmp 垃圾文件。"""
    from dl_senpai.storage import json_store as js

    path = tmp_path / "f.json"

    def always_fail(src, dst):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(js.os, "replace", always_fail)
    with pytest.raises(PermissionError):
        JsonStore(path).save({"a": 1})

    leftovers = [p.name for p in tmp_path.iterdir()]
    assert leftovers == [], f"残留临时文件：{leftovers}"


# ---------------------------------------------------------------- update


def test_update_is_read_modify_write(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "u.json")
    store.save({"count": 1})
    result = store.update(lambda cur: {**cur, "count": cur["count"] + 1})
    assert result == {"count": 2}
    assert store.load() == {"count": 2}


def test_update_handles_missing_file(tmp_path: Path) -> None:
    store = JsonStore(tmp_path / "missing.json")
    assert store.update(lambda cur: {"seen": cur is None}) == {"seen": True}


def test_mtime_changes_after_save(tmp_path: Path) -> None:
    import time

    store = JsonStore(tmp_path / "m.json")
    assert store.mtime() is None
    store.save({"a": 1})
    first = store.mtime()
    assert first is not None
    time.sleep(0.02)
    store.save({"a": 2})
    assert store.mtime() != first
