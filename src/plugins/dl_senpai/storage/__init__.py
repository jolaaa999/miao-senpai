"""统一存储层：原子写 + 加锁 + 旧格式兼容。

本阶段（Phase 2）只做基础设施与第一批迁移，不改变任何现有文件路径与
JSON 结构。外部模块可以直接::

    from .storage import JsonStore, atomic_write_text
"""

from __future__ import annotations

from .json_store import JsonStore, JsonStoreError, atomic_write_text

__all__ = ["JsonStore", "JsonStoreError", "atomic_write_text"]
