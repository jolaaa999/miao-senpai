from __future__ import annotations

import json
import time
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

    def _stamps_path(self, session_id: str) -> Path:
        """消息时间轴：与主文件同长度、按下标对齐的 ts 数组。

        为什么不直接往主文件每条里塞 ts：memory/*.json 的 role/content 结构是
        admin 控制台的读取契约，Phase 2 明确要求「JSON 结构不变」。
        时间戳另开一个文件，两边都不破。
        """
        safe = session_id_to_fs_key(session_id)
        return self.memory_dir / ".stamps" / f"{safe}.json"

    def _load_stamps(self, session_id: str, count: int) -> list[float]:
        """读时间轴。长度对不上就当没有——宁可没 ts，也不能错位盖到别人头上。"""
        try:
            data = json.loads(self._stamps_path(session_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(data, list) or len(data) != count:
            return []
        return [float(x) if isinstance(x, (int, float)) else 0.0 for x in data]

    def _save_stamps(self, session_id: str, stamps: list[float]) -> None:
        path = self._stamps_path(session_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            JsonStore(path, create_parent=False).save(stamps)
        except OSError:
            # 时间轴写不进去就降级成「这批消息没有 ts」，绝不能挡主文件保存
            pass

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
        stamps = self._load_stamps(session_id, len(data))
        cleaned: list[dict[str, str]] = []
        for idx, item in enumerate(data):
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
                msg: dict[str, Any] = {"role": item["role"], "content": content}
                # ts 只在内存里流转，不并进 content：让「读给模型」那一侧知道
                # 这条是几点的，才能判断出「这是昨天的」。旧数据没有 ts，
                # 缺了就是缺了，绝不拿当前时间补——那比没有更误导。
                _ts = item.get("ts")
                if isinstance(_ts, (int, float)) and _ts > 0:
                    msg["ts"] = float(_ts)
                elif idx < len(stamps) and stamps[idx] > 0:
                    msg["ts"] = stamps[idx]
                cleaned.append(msg)
        return cleaned

    def save(self, session_id: str, messages: list[dict[str, str]]) -> None:
        trimmed = self._apply_persist_limit(session_id, self._trim(messages))
        # 主文件只写 role/content（admin 按这个结构读），ts 走 .stamps/ 时间轴
        plain: list[dict[str, str]] = []
        stamps: list[float] = []
        for msg in trimmed:
            plain.append({"role": msg["role"], "content": msg["content"]})
            _ts = msg.get("ts")
            stamps.append(float(_ts) if isinstance(_ts, (int, float)) and _ts > 0 else 0.0)
        self._store(session_id).save(plain)
        self._save_stamps(session_id, stamps)

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
        # 每轮都盖当前时间。没有 ts 的历史等于一锅没有先后的炖菜：
        # 模型无法区分「刚说的」和「昨天说的」，会把隔夜请求当成待办接着办。
        now = time.time()
        history.append({"role": "user", "content": user_content, "ts": now})
        history.append({"role": "assistant", "content": assistant_content, "ts": now})
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


# 没有时间戳的存量消息用这个前缀：不假装知道几点，只说明「不是刚说的」。
_STALE_BARE_PREFIX = "[之前] "


def stamp_stale_messages(
    messages: list[dict[str, Any]],
    *,
    now: float | None = None,
    keep_tail: int = 2,
) -> list[dict[str, Any]]:
    """给「不是今天」的历史消息加「[10-27 21:20] 」前缀，并统一剥掉 ts 键。

    为什么必须加：历史里只有 role/content，模型看不出「帮我整个谷歌账号」是
    昨天傍晚说的，于是当成刚刚有人提、还没办完的事接着办。2026-10-28 11:34
    线上用户原话就是「你这BOT怎么还在昨天的话题」，随后它还在追着问那件事。

    只处理跨天的：同一天内的消息不加前缀，否则每一句都顶着时间戳，比不加更假。
    升级前写入的存量消息没有 ts（那时候还不记时间）。这部分不能不管：不看时间戳
    就等于把隔夜内容继续当新鲜的喂进去。历史是有序的，最后 keep_tail 条必定是
    最近两轮，其余一律标「[之前]」—— 不猜具体时刻，只表达「这不是刚说的」。

    返回值一律只留 role/content：ts 是内部字段，多一个键会让下游拼 messages 时
    报 unexpected keyword。
    """
    cur = time.time() if now is None else now
    today = time.localtime(cur)[:3]
    total = len(messages)
    out: list[dict[str, Any]] = []
    for idx, msg in enumerate(messages):
        content = msg.get("content", "")
        ts = msg.get("ts")
        if isinstance(ts, (int, float)) and ts > 0:
            lt = time.localtime(ts)
            if lt[:3] != today:
                content = time.strftime("[%m-%d %H:%M] ", lt) + content
        elif idx < total - max(0, keep_tail):
            content = _STALE_BARE_PREFIX + content
        out.append({"role": msg.get("role", "user"), "content": content})
    return out
