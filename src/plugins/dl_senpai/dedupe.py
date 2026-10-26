"""重复消息去重：同一个 message_id 只处理一次。

为什么需要：NapCat 断线重连时会重放没 ack 的事件，同一条群消息可能
进来两次。没有这层的话，她会对着同一句话回两遍——用户看到的是
「机器人抽风了」，而且第二次回复会带着已经更新过的上下文，答得还不一样。

参考实现按消息 id 判重，这块比我们原来的实现严谨。

实现取向：**只判重、不缓存结果**。命中重复就整条丢掉（连观察者都不跑），
因为观察者会往「最近聊天流水」里写，重复写进去会让她的上下文里同一句话
出现两次——那比不回更糟。
"""

from __future__ import annotations

import time
from collections import OrderedDict


class MessageDeduper:
    """按 (self_id, message_id) 判重，带 TTL 和容量上限。

    用 OrderedDict 而不是 set：既要 O(1) 查重，也要能在超容量时按
    插入顺序丢掉最老的（FIFO），set 做不到有序淘汰。
    """

    def __init__(self, *, window_sec: float = 300.0, max_items: int = 512) -> None:
        self.window_sec = max(1.0, float(window_sec))
        self.max_items = max(1, int(max_items))
        self._seen: OrderedDict[str, float] = OrderedDict()

    def _key(self, ctx: object) -> str:
        message_id = str(getattr(ctx, "message_id", "") or "").strip()
        if not message_id:
            # 没有 message_id 的消息（部分通知类事件）无从判重，
            # 返回空串让调用方直接放行，绝不能因为缺 id 就丢掉正常消息。
            return ""
        self_id = str(getattr(ctx, "self_id", "") or "").strip()
        return f"{self_id}:{message_id}"

    def is_duplicate(self, ctx: object, *, now: float | None = None) -> bool:
        """True = 这条已经处理过（或者 id 缺失以外的判重命中）。"""
        key = self._key(ctx)
        if not key:
            return False

        stamp = time.monotonic() if now is None else now
        self._evict(stamp)

        if key in self._seen:
            return True

        self._seen[key] = stamp
        # 超容量时 FIFO 淘汰：重连风暴下消息可能成百上千，不能无限涨
        while len(self._seen) > self.max_items:
            self._seen.popitem(last=False)
        return False

    def _evict(self, now: float) -> None:
        """清掉过期的 key。窗口按插入时间算，不刷新。"""
        while self._seen:
            key, stamp = next(iter(self._seen.items()))
            if now - stamp <= self.window_sec:
                break
            self._seen.popitem(last=False)

    def reset(self) -> None:
        self._seen.clear()

    def size(self) -> int:
        return len(self._seen)


_deduper: MessageDeduper | None = None


def get_deduper(
    *, window_sec: float = 300.0, max_items: int = 512
) -> MessageDeduper:
    global _deduper
    if _deduper is None:
        _deduper = MessageDeduper(window_sec=window_sec, max_items=max_items)
    return _deduper


def reset_deduper() -> None:
    """测试用：清空单例。"""
    global _deduper
    _deduper = None
