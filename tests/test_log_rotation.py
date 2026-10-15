"""子进程日志轮转（gptsovits-api.log）的回归。

背景：这个文件是外部子进程 stdout 的重定向，写入方不是我们的 logger，所以
bot.py 里的 `rotation="00:00"` + `retention="30 days"` 对它无效——它只会
一路 append。
"""

from __future__ import annotations

import inspect
from pathlib import Path

from dl_senpai.observability.logging import rotate_log_if_large


def test_rotate_moves_file_when_over_threshold(tmp_path: Path) -> None:
    log = tmp_path / "gptsovits-api.log"
    log.write_bytes(b"x" * 4096)

    backup = rotate_log_if_large(log, max_bytes=1024)

    assert backup == tmp_path / "gptsovits-api.log.1"
    assert backup is not None and backup.is_file()
    assert not log.exists(), "滚完当前文件该被挪走，让子进程从零开始写"


def test_rotate_keeps_small_file(tmp_path: Path) -> None:
    """没超阈值不许动它——否则每次启动都白滚一遍。"""
    log = tmp_path / "gptsovits-api.log"
    log.write_bytes(b"x" * 10)

    assert rotate_log_if_large(log, max_bytes=1024) is None
    assert log.read_bytes() == b"x" * 10


def test_rotate_overwrites_previous_backup(tmp_path: Path) -> None:
    """只留一个备份，否则占用还是无上限。"""
    log = tmp_path / "a.log"
    old_backup = tmp_path / "a.log.1"
    old_backup.write_bytes(b"old")
    log.write_bytes(b"x" * 4096)

    rotate_log_if_large(log, max_bytes=1024)

    assert old_backup.read_bytes() == b"x" * 4096


def test_rotate_missing_file_is_noop(tmp_path: Path) -> None:
    assert rotate_log_if_large(tmp_path / "nope.log", max_bytes=1) is None


def test_rotate_failure_does_not_raise(tmp_path: Path, monkeypatch) -> None:
    """轮转失败不能拖垮子进程启动，最多是日志继续变大。"""
    import os as _os

    log = tmp_path / "a.log"
    log.write_bytes(b"x" * 4096)

    def _boom(*a, **k):
        raise OSError("locked")

    monkeypatch.setattr(_os, "replace", _boom)
    assert rotate_log_if_large(log, max_bytes=1024) is None


def test_subprocess_server_rotates_before_open() -> None:
    """写入点必须调用轮转，且必须排在 open 之前。

    结构守卫（和项目里其它「零改动守卫」同一路子）：只要有人删掉这一行，
    日志就会重新变成无上限 append。
    """
    from dl_senpai import gptsovits_server

    src = inspect.getsource(gptsovits_server)
    assert "rotate_log_if_large(" in src, "gptsovits_server 没有调用日志轮转"
    assert src.index("rotate_log_if_large(") < src.index("open(log_path"), (
        "gptsovits_server 的轮转必须在 open 之前"
    )