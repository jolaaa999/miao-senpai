from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any

from .session_fs import session_id_to_fs_key


class ChatMemory:
    """按 session 持久化 user/assistant 消息；max_history<=0 时不裁剪。"""

    def __init__(self, memory_dir: str | Path, max_history: int = 0) -> None:
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.max_history = max_history
        self._lock = Lock()
        self._cleanup_legacy_corrupt_files()

    def _trim(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        if self.max_history <= 0:
            return messages
        return messages[-self.max_history :]

    def _cleanup_legacy_corrupt_files(self) -> None:
        """Windows 下旧版路径 group:xxx 会写成无扩展名的 group/private 空文件。"""
        for name in ("group", "private"):
            path = self.memory_dir / name
            if path.is_file() and path.stat().st_size == 0:
                try:
                    path.unlink()
                except OSError:
                    pass

    def _path(self, session_id: str) -> Path:
        safe = session_id_to_fs_key(session_id)
        return self.memory_dir / f"{safe}.json"

    def load(self, session_id: str) -> list[dict[str, str]]:
        return self._trim(self.load_all(session_id))

    def load_all(self, session_id: str) -> list[dict[str, str]]:
        """读取磁盘全量历史（不做 max_history 裁剪），供召回使用。"""
        path = self._path(session_id)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(data, list):
            return []
        cleaned: list[dict[str, str]] = []
        for item in data:
            if (
                isinstance(item, dict)
                and item.get("role") in {"user", "assistant"}
                and isinstance(item.get("content"), str)
            ):
                cleaned.append({"role": item["role"], "content": item["content"]})
        return cleaned

    def save(self, session_id: str, messages: list[dict[str, str]]) -> None:
        path = self._path(session_id)
        trimmed = self._trim(messages)
        with self._lock:
            path.write_text(
                json.dumps(trimmed, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

    def append_turn(
        self,
        session_id: str,
        user_content: str,
        assistant_content: str,
    ) -> list[dict[str, str]]:
        history = self.load(session_id)
        history.append({"role": "user", "content": user_content})
        history.append({"role": "assistant", "content": assistant_content})
        history = self._trim(history)
        self.save(session_id, history)
        return history

    def clear(self, session_id: str) -> None:
        path = self._path(session_id)
        if path.exists():
            path.unlink()


def session_id_for_group(group_id: int | str) -> str:
    return f"group:{group_id}"


def session_id_for_private(user_id: int | str) -> str:
    return f"private:{user_id}"


def messages_for_llm(history: list[dict[str, str]]) -> list[dict[str, Any]]:
    """返回可直接拼进 OpenAI messages 的历史（不含当前句）。"""
    return [{"role": m["role"], "content": m["content"]} for m in history]
