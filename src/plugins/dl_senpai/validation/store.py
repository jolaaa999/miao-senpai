"""Validation 存储：轻量、可保留期、与生产数据分离。

Phase 7A 第三十四/三十五/三十六/三十七/三十八节。

**为什么用 JSONL 而不是 SQLite**：
    第三十五节推荐 SQLite 但"不要强制"。本项目的实际情形是：
      - 写入模式是"只追加"（每次请求一条），不需要事务/索引
      - 读取由 `scripts/validation_report.py` 离线做，量级不大
      - JSONL 零依赖、可 grep、可直接用 pandas 读
    引入 SQLite 会新增 schema 迁移负担，而本阶段**只需要观察**。
    因此选 JSONL，并在需要时（数据量真的大到扫不动）
    再迁 SQLite —— 接口保持 `append()` / `read()` 两个方法，
    迁移时只换实现。

**与生产数据分离**（第三十六节）：
    默认写 `data/validation/`，与 `data/dl_senpai/`（业务数据）分开。
    绝不混进 Memory 目录，避免被 Memory 清理逻辑误删或被 Prompt 读到。
"""

from __future__ import annotations

from ..config_env import get_env, get_flag

import json
import os
import time
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DIR = "data/validation"


def _default_dir() -> Path:
    """Validation 数据目录。

    允许通过环境变量覆盖（便于测试与多实例部署）。
    """
    override = get_env("DL_SENPAI_VALIDATION_DIR", "")
    if override:
        return Path(override)
    # 相对仓库根：本文件位于 src/plugins/dl_senpai/validation/
    root = Path(__file__).resolve().parents[4]
    return root / DEFAULT_DIR


class ValidationStore:
    """追加式的验证记录存储（JSONL）。

    Args:
        directory: 数据目录。默认 `data/validation/`。
        retention_days: 保留天数（第三十七/三十八节）。
            0 表示不清理。默认 30 天 —— **可配置**，不硬编码业务决定。
        max_file_mb: 单文件大小上限（MB）。超过则滚动到新文件。
        enabled: 是否落盘。默认由环境变量控制，
            未显式打开时**不写任何文件**（避免开发机产生垃圾）。
    """

    def __init__(
        self,
        directory: Path | str | None = None,
        *,
        retention_days: int = 30,
        max_file_mb: int = 64,
        enabled: bool | None = None,
    ) -> None:
        self.directory = Path(directory) if directory else _default_dir()
        self.retention_days = max(0, int(retention_days))
        self.max_file_bytes = max(1, int(max_file_mb)) * 1024 * 1024
        if enabled is None:
            enabled = get_flag("DL_SENPAI_VALIDATION")
        self.enabled = bool(enabled)
        # 进程内缓存当前文件名，避免每次 stat
        self._current: Path | None = None

    # -- 写入 --

    def append(self, record: Any) -> bool:
        """追加一条记录。返回是否真的写入。

        `record` 只要有 `as_dict()` 即可（结构化协议，不强制继承）。
        """
        if not self.enabled:
            return False
        payload = record.as_dict() if hasattr(record, "as_dict") else dict(record)
        if "timestamp" not in payload or not payload["timestamp"]:
            payload["timestamp"] = time.time()

        self.directory.mkdir(parents=True, exist_ok=True)
        target = self._current_file()
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        # 原子追加：单行写入（POSIX/Windows 下小行追加不会撕裂）
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        return True

    def _current_file(self) -> Path:
        """取当前应写入的文件（超过大小上限则滚动）。"""
        today = time.strftime("%Y%m%d")
        if self._current is not None and self._current.name.startswith(
            f"validation-{today}"
        ):
            try:
                if self._current.stat().st_size < self.max_file_bytes:
                    return self._current
            except OSError:
                pass

        # 找到当天下一个可用序号
        index = 0
        while True:
            suffix = "" if index == 0 else f"-{index}"
            candidate = self.directory / f"validation-{today}{suffix}.jsonl"
            if not candidate.exists() or candidate.stat().st_size < self.max_file_bytes:
                self._current = candidate
                return candidate
            index += 1
            if index > 999:  # 防御：单日超过 1000 个分片说明配置有问题
                self._current = candidate
                return candidate

    # -- 读取 --

    def read(
        self,
        *,
        since: float | None = None,
        until: float | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """读取记录（按时间过滤）。"""
        out: list[dict[str, Any]] = []
        for payload in self.iter_records(since=since, until=until):
            out.append(payload)
            if limit is not None and len(out) >= limit:
                break
        return out

    def iter_records(
        self, *, since: float | None = None, until: float | None = None
    ) -> Iterator[dict[str, Any]]:
        """逐条迭代（不一次性加载，适合大文件）。"""
        if not self.directory.exists():
            return
        for path in sorted(self.directory.glob("validation-*.jsonl")):
            try:
                fh = open(path, encoding="utf-8")
            except OSError:
                continue
            with fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except json.JSONDecodeError:
                        # 半行写入（进程被杀）——跳过坏行而不是整体失败
                        continue
                    ts = payload.get("timestamp") or 0
                    if since is not None and ts < since:
                        continue
                    if until is not None and ts > until:
                        continue
                    yield payload

    # -- 保留期清理（第三十七节）--

    def cleanup(self, *, now: float | None = None) -> list[str]:
        """删除超过保留期的数据文件。返回被删除的文件名列表。

        按**文件修改时间**判断（文件名里的日期也可能因时区/改名不准）。
        """
        if self.retention_days <= 0 or not self.directory.exists():
            return []
        cutoff = (now if now is not None else time.time()) - (
            self.retention_days * 86400
        )
        removed: list[str] = []
        for path in sorted(self.directory.glob("validation-*.jsonl")):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed.append(path.name)
            except OSError:
                continue
        return removed

    def stats(self) -> dict[str, Any]:
        """存储概况（供报告与运维查看）。"""
        files = (
            sorted(self.directory.glob("validation-*.jsonl"))
            if self.directory.exists()
            else []
        )
        total_bytes = 0
        for path in files:
            try:
                total_bytes += path.stat().st_size
            except OSError:
                continue
        return {
            "enabled": self.enabled,
            "directory": str(self.directory),
            "files": len(files),
            "bytes": total_bytes,
            "retention_days": self.retention_days,
            "max_file_mb": self.max_file_bytes // (1024 * 1024),
        }


__all__ = ["ValidationStore", "DEFAULT_DIR"]
