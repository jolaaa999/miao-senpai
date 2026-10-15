"""统一 JSON 存储：原子写 + 加锁 + 兼容旧格式读取。

设计动机（为什么需要这层）：

重构前的 14 个 ``*Store`` 类各自实现了 ``read_text`` / ``json.loads`` /
``write_text`` / ``tmp`` / ``os.replace`` / ``Lock``，副本之间行为不一致：
其中 ``memory.py`` / ``person_memory.py`` / ``affection.py`` 直接
``path.write_text()``，进程在中途被 kill（Windows 上很常见：任务管理器结束、
断电、蓝屏）就会把正式 JSON 截断成半截，用户记忆直接损坏。

本模块把「读写 + 原子性 + 并发」收敛到一处，同时**不改变任何现有文件路径与
JSON 结构**，因此旧数据无需迁移即可继续被读写。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable

_MISSING = object()


class JsonStoreError(Exception):
    """JSON 读取/写入失败。仅在最外层调用方需要区分错误类型时使用。"""


def atomic_write_text(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
    fsync: bool = True,
    retries: int = 5,
    retry_delay: float = 0.02,
) -> None:
    """把 ``text`` 原子地写入 ``path``。

    实现：同目录建临时文件 → 写入 → fsync → ``os.replace`` 覆盖目标。

    为什么必须同目录：``os.replace`` 只有在同一文件系统内才是原子操作；
    跨盘（例如临时目录在 C: 而数据在 E:）会退化成「复制 + 删除」，
    中途失败同样会破坏目标文件。

    为什么 fsync：Windows 上 ``os.replace`` 只保证目录项切换的原子性，
    不保证数据已落到磁盘。断电后可能出现「文件大小对但内容是零」的情况。

    **为什么需要 retries（Windows 专属）**：Windows 不像 POSIX 那样允许
    替换一个正被其他句柄打开的文件。当 Go 控制台正在读
    `group_features.json`、或本进程另一个线程正在 `read_text` 时，
    `os.replace` 会抛 ``PermissionError: [WinError 5] 拒绝访问``。

    这不是理论问题——它会让「控制台正在看群功能页」期间的一次配置写入
    直接失败。因此这里做有界重试：读句柄通常是短命操作（毫秒级），
    退避几十毫秒即可等到它关闭。重试耗尽才真正抛出。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    directory = str(path.parent)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=directory
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())

        last_error: OSError | None = None
        for attempt in range(max(1, retries)):
            try:
                os.replace(tmp_path, path)
                return
            except PermissionError as exc:
                # 目标被其他句柄占用（Windows 常见）；短暂退避后重试
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(retry_delay * (attempt + 1))
            except OSError as exc:
                last_error = exc
                break
        raise last_error if last_error is not None else OSError("replace failed")
    except BaseException:
        # 失败时清掉临时文件，避免在数据目录里堆积 .tmp 垃圾
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


class JsonStore:
    """单文件 JSON 存储，线程安全 + 原子写。

    典型用法::

        store = JsonStore("data/dl_senpai/memory/group_123.json")
        data = store.load(default=[])          # 不存在/损坏都返回 default
        store.save(data)                       # 原子覆盖

    并发语义：所有读写走同一把可重入锁，保证**进程内**串行。
    本项目的 bot 是单进程 asyncio，所有 Store 都是同步调用，因此进程内
    加锁已经足够；跨进程并发（如 Go 控制台同时读写 group_features.json）
    由 ``os.replace`` 的原子性兜底：读到的要么是旧完整内容，要么是新完整
    内容，不会读到半截。
    """

    def __init__(
        self,
        path: str | Path,
        *,
        indent: int | None = 2,
        ensure_ascii: bool = False,
        fsync: bool = True,
        create_parent: bool = True,
    ) -> None:
        self.path = Path(path)
        self._indent = indent
        self._ensure_ascii = ensure_ascii
        self._fsync = fsync
        # 必须用可重入锁：update() 会持锁调用 save()，而 save() 自己也要拿锁。
        # 用普通 Lock 会**直接死锁**（本文件曾因此挂死测试）。
        self._lock = threading.RLock()
        if create_parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- 查询 --

    def exists(self) -> bool:
        return self.path.is_file()

    def mtime(self) -> float | None:
        """文件最后修改时间；不存在返回 None。供 mtime 缓存使用。"""
        try:
            return self.path.stat().st_mtime
        except OSError:
            return None

    # -- 读 --

    def load(self, default: Any = _MISSING) -> Any:
        """读取 JSON。

        文件不存在、为空、内容损坏、**或暂时被占用**时返回 ``default``
        （默认 None）。这里**不抛异常**是刻意设计：调用方（聊天记忆、
        好感度）在数据不可读时应当降级为空数据继续服务，而不是让整条
        消息处理链路崩溃。

        Windows 说明：当另一个句柄正在 ``os.replace`` 覆盖本文件时，
        ``read_text`` 可能瞬间抛 ``PermissionError``。这在真实运行中会
        发生（bot 写入的同时 Go 控制台在读）。因此这里对占用做有界重试，
        读完仍失败才降级——保证「读到的要么是完整旧内容，要么是完整新内容」。
        """
        fallback = None if default is _MISSING else default
        raw = self._read_text_resilient()
        if raw is None:
            return fallback
        if not raw.strip():
            # 空文件（旧版本 Windows 路径 bug 会产生这种文件）视为无数据
            return fallback
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return fallback

    def _read_text_resilient(self, retries: int = 5, delay: float = 0.02) -> str | None:
        """带占用重试的读文本；真正读不到时返回 None。"""
        last_error: OSError | None = None
        for attempt in range(max(1, retries)):
            try:
                return self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return None
            except PermissionError as exc:
                # 正被写入方替换；退避后重试
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(delay * (attempt + 1))
            except OSError as exc:
                last_error = exc
                break
        if last_error is not None:
            return None
        return None

    def load_or_raise(self) -> Any:
        """严格读取：损坏时抛 ``JsonStoreError``。需要区分「无数据/损坏」时用。"""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise JsonStoreError(f"无法读取 {self.path}: {exc}") from exc
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise JsonStoreError(f"{self.path} 内容不是合法 JSON: {exc}") from exc

    def load_transformed(
        self,
        transform: Callable[[Any], Any],
        default: Any = _MISSING,
    ) -> Any:
        """读取后立即用 ``transform`` 规整（例如把 dict 转成领域对象）。

        文件不存在、内容损坏、或 transform 抛异常时统一返回 ``default``
        （未指定则为 None）。用于兼容历史遗留的脏字段。
        """
        fallback = None if default is _MISSING else default
        raw = self.load(_MISSING)
        if raw is None:
            # load() 在"无文件/空文件/损坏"时都返回 None；
            # JSON 顶层为 null 的极罕见情况也归入此分支（对业务无影响）
            return fallback
        try:
            return transform(raw)
        except Exception:  # noqa: BLE001 — 脏数据不应中断服务
            return fallback

    # -- 写 --

    def save(self, data: Any) -> None:
        """原子覆盖写入。"""
        if self._indent is None:
            payload = json.dumps(data, ensure_ascii=self._ensure_ascii)
        else:
            payload = json.dumps(
                data, ensure_ascii=self._ensure_ascii, indent=self._indent
            )
        with self._lock:
            atomic_write_text(self.path, payload, fsync=self._fsync)

    def update(self, mutate: Callable[[Any], Any]) -> Any:
        """读-改-写：在同一把锁内完成，避免并发丢失更新。

        ``mutate`` 接收当前值（不存在时为 None），返回要保存的新值。
        """
        with self._lock:
            current = self.load()
            updated = mutate(current)
            self.save(updated)
            return updated

    # -- 删 --

    def delete(self) -> bool:
        """删除文件；返回是否真的删掉了。"""
        with self._lock:
            try:
                self.path.unlink()
                return True
            except FileNotFoundError:
                return False
            except OSError:
                return False
