"""最近聊天缓冲：记录**所有**消息（含她没参与的），供「今天聊过什么」召回。

为什么需要单独一层：
    ChatMemory 只记「她参与过」的轮次（回复发送成功后才落盘），群里别人
    互相聊的内容**从来不进任何存储**。而她被 @ 问「你怎么看」时，恰恰需要
    那段背景。所以这层独立记录、独立存储。

为什么必须落盘：
    窗口是 24 小时。bot 重启很频繁（改配置、崩溃、手动重启），纯内存的话
    每次重启都清零，「一天」就名存实亡。所以走 JsonStore 原子写。

为什么不把一整天都塞进 prompt：
    活跃群一天上千条，全塞进去是几万 token——既超预算，也会把她的判断力
    冲散。所以**存储按天、注入按预算**：最近 N 条 + 当天内按关键词召回
    M 条。召回复用 memory_recall 的 n-gram 打分，不另起一套。

与 ChatMemory 的分工：
    ChatMemory   —— 她参与的对话（user/assistant 成对），进 messages
    RecentChat   —— 全部消息的流水（含无关闲聊），只进 system prompt 的一段
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock
from typing import Any

from .session_fs import session_id_to_fs_key
from .storage import JsonStore

# 默认保留 24 小时
DEFAULT_TTL_SEC = 24 * 3600.0
# 单会话内存中最多留多少条。一天上千条的群也不会无限涨。
DEFAULT_MAX_ENTRIES = 2000
# 刷盘节流：每条消息都重写整个文件的话，一天要写几 MB 的无用 IO。
DEFAULT_FLUSH_INTERVAL = 5.0


@dataclass(frozen=True)
class ChatEntry:
    """一条聊天流水。ts 用 Unix 秒，渲染时才转成本地时间。"""

    ts: float
    sender: str
    user_id: str
    text: str
    # 用于把「当前这条触发消息」从背景里剔掉，避免和本句重复
    message_id: str = ""


def _fmt_clock(ts: float) -> str:
    """把时间戳渲染成 HH:MM。库里存的是绝对时间，显示才转本地。"""
    try:
        return time.strftime("%H:%M", time.localtime(float(ts)))
    except (ValueError, OSError, TypeError):
        return ""


class RecentChatStore:
    """按 session 保存最近一天的全部消息。

    并发语义与 ChatMemory 一致：单进程 asyncio，同步方法 + 一把锁。
    刷盘是被节流的——内存是运行时的事实来源，磁盘只负责跨重启续命。
    """

    def __init__(
        self,
        root: str | Path,
        *,
        ttl_sec: float = DEFAULT_TTL_SEC,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        flush_interval: float = DEFAULT_FLUSH_INTERVAL,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.ttl_sec = max(60.0, float(ttl_sec))
        self.max_entries = max(1, int(max_entries))
        self.flush_interval = max(0.0, float(flush_interval))
        self._lock = Lock()
        self._cache: dict[str, list[ChatEntry]] = {}
        self._dirty: set[str] = set()
        self._last_flush: dict[str, float] = {}

    # -- 路径 --

    def _path(self, session_id: str) -> Path:
        return self.root / f"{session_id_to_fs_key(session_id)}.json"

    # -- 读 --

    def _prune(self, items: list[ChatEntry], now: float) -> list[ChatEntry]:
        """裁掉过期条目并截断到 max_entries（保留最新的）。"""
        cutoff = now - self.ttl_sec
        fresh = [e for e in items if e.ts >= cutoff]
        if len(fresh) > self.max_entries:
            fresh = fresh[-self.max_entries :]
        return fresh

    def _load(self, session_id: str) -> list[ChatEntry]:
        """从磁盘读一次；惰性调用，读不到就当空。"""
        raw = JsonStore(self._path(session_id)).load(default=[])
        if not isinstance(raw, list):
            return []
        out: list[ChatEntry] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            text = item.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            try:
                ts = float(item.get("ts") or 0.0)
            except (TypeError, ValueError):
                ts = 0.0
            out.append(
                ChatEntry(
                    ts=ts,
                    sender=str(item.get("sender") or ""),
                    user_id=str(item.get("user_id") or ""),
                    text=text,
                    message_id=str(item.get("message_id") or ""),
                )
            )
        out.sort(key=lambda e: e.ts)
        return out

    def entries(self, session_id: str, *, now: float | None = None) -> list[ChatEntry]:
        """返回该会话一天内的消息（时间升序）。"""
        if not session_id:
            return []
        moment = time.time() if now is None else now
        with self._lock:
            if session_id not in self._cache:
                self._cache[session_id] = self._load(session_id)
            items = self._prune(self._cache[session_id], moment)
            self._cache[session_id] = items
            return list(items)

    # -- 写 --

    def mark_recalled(
        self, session_id: str, message_id: str, *, marker: str = "（已撤回）"
    ) -> bool:
        """把某条流水标成「已撤回」；命中返回 True。

        为什么标注而不是删除：她需要知道「那句话不作数了」，但删掉会让上下文出现断层
        （前一句的回应突然失去所指）。留一行标记，模型能自己理解成"对方收回了那句话"。
        """
        if not session_id or not message_id:
            return False
        with self._lock:
            items = self._cache.get(session_id)
            if items is None:
                items = self._load(session_id)
                self._cache[session_id] = items
            # ChatEntry 是 frozen 的（内存里的历史不允许被就地改写），
            # 所以这里用 replace 造一条新的塞回列表，而不是给 text 赋值。
            for idx, entry in enumerate(items):
                if entry.message_id != message_id:
                    continue
                if marker not in entry.text:
                    items[idx] = replace(entry, text=entry.text.rstrip() + marker)
                    self._dirty.add(session_id)
                return True
            return False

    def append(
        self,
        session_id: str,
        *,
        sender: str,
        user_id: str = "",
        text: str,
        message_id: str = "",
        ts: float | None = None,
    ) -> None:
        """记一条消息。空文本直接丢——空白消息进背景只会浪费预算。"""
        if not session_id:
            return
        body = (text or "").strip()
        if not body:
            return
        moment = time.time() if ts is None else float(ts)
        entry = ChatEntry(
            ts=moment,
            sender=(sender or "").strip() or user_id or "某人",
            user_id=str(user_id or ""),
            text=body,
            message_id=str(message_id or ""),
        )
        with self._lock:
            items = self._cache.get(session_id)
            if items is None:
                items = self._load(session_id)
            items.append(entry)
            self._cache[session_id] = self._prune(items, moment)
            self._dirty.add(session_id)

    # -- 刷盘 --

    def flush(self, *, force: bool = False, now: float | None = None) -> int:
        """把脏会话写盘，返回实际写出的会话数。

        节流的意义：群里每分钟几十条消息，若每条都重写整个 JSON，
        一天的写放大很可观。默认 5 秒最多写一次；force=True 供停机时用。
        """
        moment = time.time() if now is None else now
        written = 0
        with self._lock:
            for session_id in list(self._dirty):
                last = self._last_flush.get(session_id, 0.0)
                if not force and moment - last < self.flush_interval:
                    continue
                payload = [
                    {
                        "ts": e.ts,
                        "sender": e.sender,
                        "user_id": e.user_id,
                        "text": e.text,
                        "message_id": e.message_id,
                    }
                    for e in self._cache.get(session_id, [])
                ]
                try:
                    JsonStore(self._path(session_id)).save(payload)
                except Exception:  # noqa: BLE001 — 写不进去不该拖垮消息链路
                    continue
                self._dirty.discard(session_id)
                self._last_flush[session_id] = moment
                written += 1
        return written

    def forget(self, session_id: str) -> None:
        """清掉一个会话（供 reset 命令用）。"""
        with self._lock:
            self._cache.pop(session_id, None)
            self._dirty.discard(session_id)
            self._last_flush.pop(session_id, None)
        try:
            self._path(session_id).unlink()
        except OSError:
            pass


# -- 渲染 --

def build_recent_chat_brief(
    entries: list[ChatEntry],
    query: str,
    *,
    recent_n: int = 20,
    recall_n: int = 10,
    max_chars: int = 2000,
    exclude_message_id: str = "",
    is_private: bool = False,
) -> str:
    """把流水渲染成给模型看的一段背景。

    结构：
        最近 recent_n 条（保「刚聊的」）+ 更早的 recall_n 条关键词命中
        （保「上午提过的」）。

    为什么两段都要：只给最近 N 条，群里刷屏快，十几秒就把话题冲掉；
    只给召回，又接不上「刚才那句」。两者互补。
    """
    if not entries:
        return ""

    pool = [e for e in entries if not (exclude_message_id and e.message_id == exclude_message_id)]
    if not pool:
        return ""

    heads = pool[-max(1, int(recent_n)) :]
    head_ids = {id(e) for e in heads}
    older = [e for e in pool if id(e) not in head_ids]

    # 更早的部分按当前话题召回。复用 memory_recall 的词提取与打分，
    # 保持和「相关旧聊天」同一套口径，避免两处标准打架。
    picked_old: list[ChatEntry] = []
    want = max(0, int(recall_n))
    if want and older:
        try:
            from .memory_recall import extract_keywords, score_text

            keywords = extract_keywords(query or "")
            if keywords:
                scored = [(score_text(e.text, keywords), i, e) for i, e in enumerate(older)]
                scored = [item for item in scored if item[0] > 0]
                # 分数高优先；同分取更近的（下标大）
                scored.sort(key=lambda x: (-x[0], -x[1]))
                picked_old = [e for _s, _i, e in scored[:want]]
                picked_old.sort(key=lambda e: e.ts)
        except Exception:  # noqa: BLE001 — 召回失败就只给最近那段
            picked_old = []

    title = "今天聊过的" if is_private else "今天群里聊过的"
    lines: list[str] = [
        f"【{title}】（背景资料，别复读；接得上话就行，不确定就别硬接）",
        # 为什么要点明格式：每行是「- 时间 发言人：内容」，冒号前是**人名**。
        # 不写这句时模型会把名字当成正文的一部分——实测「失语.：123」被拆成
        # 「失语.」是个名字？还是在说你自己无语了？，因为「失语」本身是个常用词。
        "- 每行格式：时间 发言人：内容。冒号前是发言人名字，不是正文。",
    ]
    total = 0

    def _emit(tag: str, items: list[ChatEntry]) -> None:
        nonlocal total
        if not items:
            return
        # 标签和 total 无关：第一段也可能就是「更早的相关」那次调用。
        # 之前写成 if tag and total，第一段时 total==0，标签被直接吞掉。
        if tag:
            lines.append(tag)
            total += len(tag)
        for e in items:
            stamp = _fmt_clock(e.ts)
            who = e.sender or e.user_id or "某人"
            body = e.text.replace("\n", " ").strip()
            line = f"- {stamp} {who}：{body}" if stamp else f"- {who}：{body}"
            room = max_chars - total
            if room <= 0:
                return
            if len(line) > room:
                line = line[: max(0, room - 1)] + "…"
            lines.append(line)
            total += len(line)

    if picked_old:
        _emit("（更早、和刚才话题相关的）", picked_old)
    _emit("", heads)

    # 只有说明行（标题 + 格式说明）就说明一条都没进去，视为没有背景。
    # 数字要跟着说明行数走：加了格式说明那行之后还写 <= 1，空的流水也会被当成
    # 「有背景」注入，模型会对着两行空说明硬编。
    if len(lines) <= 2:
        return ""
    return "\n".join(lines)


# -- 进程级单例 --

_store: RecentChatStore | None = None
_store_root: str | None = None


def get_recent_chat_store(root: str | Path | None = None) -> RecentChatStore:
    """取进程级单例。

    为什么用单例：写入端（Observer）和读取端（legacy_bridge）在两条不同
    的调用链上，必须共享同一份内存态，否则刚记下的消息读不到。
    根目录变化时（测试里换临时目录）重建。
    """
    global _store, _store_root
    key = str(root) if root else None
    if _store is None or key != _store_root:
        _store = RecentChatStore(root or "data/dl_senpai/recent_chat")
        _store_root = key
    return _store
