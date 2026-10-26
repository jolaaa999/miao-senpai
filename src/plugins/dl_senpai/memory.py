from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Any

from .session_fs import session_id_to_fs_key
from .storage import JsonStore
# 读历史时清理标记残渣，与发送前的 polish_reply 共用同一套规则
from .style import strip_marker_residue


class ChatMemory:
    """按 session 持久化 user/assistant 消息；max_history<=0 时不裁剪。

    Phase 2 改动：写入改走 `JsonStore`（原子写）而不是直接
    `path.write_text()`。**对外接口、文件路径、JSON 结构全部不变**，
    已有 `data/dl_senpai/memory/*.json` 无需迁移即可继续读取。

    为什么必须改：`write_text` 是"先清空再写"，进程在写入途中被 kill
    （Windows 上任务管理器结束、断电）会留下半截 JSON，用户全部聊天记忆
    丢失。原子写保证"要么旧内容、要么新内容"。
    """

    # 字符预算下最少保留的条数：即使单条特别长，也不能把最后一轮砍掉，
    # 否则她会「刚说完就忘」，比上下文长更糟。
    # 只保 2 条（一问一答）：保得多，小预算就等于完全失效。
    _MIN_KEEP_MESSAGES = 2

    def __init__(
        self,
        memory_dir: str | Path,
        max_history: int = 0,
        max_chars: int = 0,
        persist_max: int = 0,
    ) -> None:
        self.memory_dir = Path(memory_dir)
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        self.max_history = max_history
        # 喂给模型的字符预算（0 = 不限）。实测某个私聊会话攒到 289 条 / 3.2 万字符，
        # 光是历史就把上下文塞满：白烧 token，还把她的注意力稀释到早期内容上。
        # 注意预算**只在读取时生效**，不写回磁盘——否则等于把用户记忆慢慢删掉。
        self.max_chars = max_chars
        # 落盘侧滚动上限（条数，0 = 不限）。
        # 为什么需要它：max_history 只裁剪「读给模型」那一份，磁盘文件仍然无限长，
        # 实测有会话攒到 343 条。文件越长，每轮 append 都要整份重写；
        # 更麻烦的是旧人设时期的回复原文会一直躺在里面被反复读到。
        self.persist_max = max(0, int(persist_max))
        self._lock = Lock()
        self._cleanup_legacy_corrupt_files()

    def _trim(self, messages: list[dict[str, str]]) -> list[dict[str, str]]:
        if self.max_history <= 0:
            return messages
        return messages[-self.max_history :]

    def _apply_char_budget(
        self, messages: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        """从最新往回累加字符，超预算就丢掉最老的。

        必须从最新往回数：丢最老的那端才不会破坏 user/assistant 交替的近因。
        """
        if self.max_chars <= 0 or not messages:
            return messages

        total = 0
        kept = 0
        for msg in reversed(messages):
            cost = len(msg.get("content", ""))
            # 已经满足最少保留条数、且再加就超预算 → 停
            if kept >= self._MIN_KEEP_MESSAGES and total + cost > self.max_chars:
                break
            total += cost
            kept += 1
        if kept >= len(messages):
            return messages
        return messages[-kept:]

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

    def _store(self, session_id: str) -> JsonStore:
        return JsonStore(self._path(session_id), create_parent=False)

    def _rollover_path(self, session_id: str) -> Path:
        safe = session_id_to_fs_key(session_id)
        return self.memory_dir / ".rollover" / f"{safe}.jsonl"

    def _archive_rolled_over(self, session_id: str, dropped: list[dict[str, str]]) -> None:
        """把被滚出上限的旧消息追加到归档文件，而不是直接丢掉。

        为什么不直接删：这些是用户真实聊过的内容，将来做长期记忆提炼、
        或者用户问「我们以前是不是聊过 X」的时候还要用。归档是 append-only，
        成本很低；真要清理，删 .rollover 目录就行。
        """
        if not dropped:
            return
        path = self._rollover_path(session_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f:
                for msg in dropped:
                    f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except OSError:
            # 归档失败不能挡住保存：宁可丢归档，也不能让这一轮记忆写不进去
            pass

    def _apply_persist_limit(
        self, session_id: str, messages: list[dict[str, str]]
    ) -> list[dict[str, str]]:
        """把超出滚动上限的老消息挪进归档，只留最近的。"""
        if self.persist_max <= 0 or len(messages) <= self.persist_max:
            return messages
        dropped = messages[: len(messages) - self.persist_max]
        self._archive_rolled_over(session_id, dropped)
        return messages[-self.persist_max :]

    def load(self, session_id: str) -> list[dict[str, str]]:
        """读给模型用的历史：先按条数裁剪，再按字符预算裁剪。"""
        return self._apply_char_budget(self._trim(self.load_all(session_id)))

    def load_all(self, session_id: str) -> list[dict[str, str]]:
        """读取磁盘全量历史（不做 max_history 裁剪），供召回使用。"""
        data = self._store(session_id).load(default=[])
        if not isinstance(data, list):
            return []
        cleaned: list[dict[str, str]] = []
        for item in data:
            if (
                isinstance(item, dict)
                and item.get("role") in {"user", "assistant"}
                and isinstance(item.get("content"), str)
            ):
                # 历史里残留的工具标记必须在这里剥掉：这类残渣一旦被喂回
                # prompt，模型会把它当成「上一轮没答完的题」接着作答 ——
                # 2026-10-15 线上那条 <<<CODE ... <<<END>>> 就是这样，导致
                # 之后每一轮都在补答同一个旧问题（用户侧就是"答非所问"）。
                content = strip_marker_residue(item["content"])
                if item["role"] == "assistant" and not content.strip():
                    # 整条就是标记残渣：不进上下文，但也不改用户数据文件
                    continue
                cleaned.append({"role": item["role"], "content": content})
        return cleaned

    def save(self, session_id: str, messages: list[dict[str, str]]) -> None:
        trimmed = self._apply_persist_limit(session_id, self._trim(messages))
        self._store(session_id).save(trimmed)

    def append_turn(
        self,
        session_id: str,
        user_content: str,
        assistant_content: str,
    ) -> list[dict[str, str]]:
        # 刻意用 _trim(load_all()) 而不是 load()：load() 会套字符预算，
        # 而这里的返回值还要 save() 回磁盘——套了预算就等于每轮都删掉
        # 一段用户历史。预算只作用于「读给模型」那一侧。
        history = self._trim(self.load_all(session_id))
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
