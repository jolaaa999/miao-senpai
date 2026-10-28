"""消息合并窗口：把连着发的几条攒成一次再回。

搬自 ai-companion-kit 的 wake.js::batch + index.js 第 137-145 行。

它解决的场景：

    小李：那个 PPT
    小李：就是上次说的
    小李：你有吗
            ↓ 真人会当成一句话理解，等他说完再回
          而不是追着回三次「哪个 PPT」「上次哪个」「有没有」

注意这跟「分条发送」是两件事，别混：
    - 分条发送（bubble_max）：她**说**的话被拆成几条 —— 已经默认关掉；
    - 合并窗口（本模块）：对方**发**的几条被攒成一批 —— 就是这里。

实现是标准 debounce：每条消息都睡一个窗口，醒来时看窗口有没有被后来的人
推后。被推后 → 让最后那个人去回，自己直接退出；没被推后 → 说明自己是这一串
的最后一条，合并后交出去。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class _Buffer:
    """一个会话正在攒的那一串消息。"""

    items: list[tuple[str, str]] = field(default_factory=list)  # (说话人, 正文)
    deadline: float = 0.0


class MessageBatcher:
    """按会话攒消息。只放内存，不落盘。"""

    def __init__(self, *, max_items: int = 6) -> None:
        # 上限仿参考实现（6 条）：真有人刷屏时不能无限等下去
        self.max_items = max(1, int(max_items))
        self._buffers: dict[str, _Buffer] = {}

    def _peek(self, session_id: str) -> _Buffer:
        buf = self._buffers.get(session_id)
        if buf is None:
            buf = _Buffer()
            self._buffers[session_id] = buf
        return buf

    async def wait_and_merge(
        self,
        session_id: str,
        text: str,
        actor: str,
        *,
        window_ms: int,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> str | None:
        """把本条塞进窗口并等待。

        返回 None 表示「后面还有人在说，这一条不用单独回」，由最后那条负责；
        返回字符串表示窗口到点、这是合并后的完整正文（只有一条时原样返回）。
        """
        buf = self._peek(session_id)
        buf.items.append((actor or "", (text or "").strip()))
        window = max(0.0, float(window_ms) / 1000.0)
        # 窗口从最近一条开始重新计时——这就是 debounce 的核心。
        #
        # 为什么用 max 而不是直接覆盖：窗口长度可以**逐条不同**（对方那句明显
        # 没说完时，presence 会给它一个长得多的等待窗口）。直接覆盖的话，
        # 后面跟着的一条「完整」消息会把长窗口压回 2~3 秒，等下文就白等了。
        # 用 max 让长窗口粘住，最后由还在睡的那个任务在长窗口到点时把合并结果
        # 交出去（短窗口的任务醒来发现没到点，直接安静退出）。
        buf.deadline = max(buf.deadline, now_fn() + window)

        if len(buf.items) >= self.max_items:
            # 攒满了不再等满窗口，直接交出去，避免有人刷屏时一直不吭声
            return self._flush(session_id)

        if window > 0:
            await asyncio.sleep(window)

        # 醒来发现窗口又被推后了：说明后面还有消息，交给最后那条处理
        if now_fn() < buf.deadline:
            return None
        return self._flush(session_id)

    def _flush(self, session_id: str) -> str | None:
        buf = self._buffers.pop(session_id, None)
        if buf is None or not buf.items:
            return None
        items = [(a, t) for a, t in buf.items if t]
        if not items:
            return None
        if len(items) == 1:
            return items[0][1]
        # 多人时才带说话人前缀：同一个人连打三行还标三遍名字，读起来很吵
        actors = {a for a, _ in items if a}
        if len(actors) <= 1:
            return "\n".join(t for _, t in items)
        return "\n".join(f"{a}：{t}" if a else t for a, t in items)

    def pending(self, session_id: str) -> int:
        buf = self._buffers.get(session_id)
        return len(buf.items) if buf else 0

    def reset(self) -> None:
        self._buffers.clear()


_batcher: MessageBatcher | None = None


def get_batcher(max_items: int = 6) -> MessageBatcher:
    global _batcher
    if _batcher is None:
        _batcher = MessageBatcher(max_items=max_items)
    return _batcher


def reset_batcher() -> None:
    """测试用：清空单例（含正在攒的消息）。"""
    global _batcher
    _batcher = None
