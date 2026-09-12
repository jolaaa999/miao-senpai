"""Session ID 与文件系统安全名互转（Windows 文件名不能含 ':'）。"""

from __future__ import annotations

import re

_FS_SAFE_RE = re.compile(r"[^\w\-.]")


def session_id_to_fs_key(session_id: str) -> str:
    """group:123 -> group_123；private:9 -> private_9"""
    return _FS_SAFE_RE.sub("_", session_id)


def fs_key_to_session_id(fs_key: str) -> str:
    """group_123 -> group:123（仅处理 group_/private_ 前缀）。"""
    if fs_key.startswith("group_"):
        return "group:" + fs_key[len("group_") :]
    if fs_key.startswith("private_"):
        return "private:" + fs_key[len("private_") :]
    return fs_key.replace("_", ":", 1) if "_" in fs_key else fs_key
