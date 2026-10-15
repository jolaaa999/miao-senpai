"""有界 TTL 字典：给各处「内存里只会长不会减」的缓存做治理。

Phase 1 审计发现的泄漏点（全部是长期运行才暴露，短测看不出来）：

- `trigger.InterruptCooldown._last`      —— 每个 session 一条，永不删除
- `trigger.AttentionWindow._armed`       —— 每个 (session,user) 一条
- `group_features._name_fetch_ts`        —— 每个群一条
- `image_gen.DrawCooldown` / `browser_agent.BrowseCooldown`
  / `group_card.CardCooldown` / `group_mute.MuteCooldown` —— 同上

这些 dict 的 key 空间由「群号 × QQ 号」决定。个人 bot 挂机数月、群成员
流动的情况下，内存会缓慢但确定地增长，而且**永远不会释放**。

设计要点：**不得影响冷却语义**。
- `get()` 读过期项返回 None（等价于"没记录过"，冷却判定自然放行）；
- 惰性清理在写入时顺带做，不引入后台线程/定时任务；
- 超过硬上限时按"最久未更新"淘汰（LRU）。

关键约束：TTL 必须**大于**对应冷却窗口，否则会在冷却期内把记录清掉，
导致冷却被意外绕过。因此每个使用点传的 ttl 都要显著大于 cooldown_sec。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Iterator


class BoundedTTLMap:
    """带 TTL 与容量上限的字典。

    Args:
        ttl: 条目存活秒数；<=0 表示不过期（此时只受 max_size 约束）。
        max_size: 硬上限，超出按最久未更新淘汰。
        clock: 时间源，测试可注入假时钟。
    """

    def __init__(
        self,
        *,
        ttl: float = 0.0,
        max_size: int = 4096,
        clock: Any = None,
    ) -> None:
        self._ttl = float(ttl)
        self._max_size = max(1, int(max_size))
        # 默认用 time.time()（墙钟），与各处冷却判定传入的 `now` 保持同一时基。
        #
        # 为什么不用 monotonic：冷却类（SessionCooldown / InterruptCooldown）
        # 的 ready() 默认走 `time.time()`，而时间戳由本 map 记录。
        # 若这里用 monotonic（进程启动起算，量级 ~1e5），
        # 与 time.time()（Unix 纪元，量级 ~1.7e9）相减会得到巨大正值，
        # 导致**冷却永远判定为已就绪**——冷却功能静默失效。
        self._clock = clock or time.time
        self._data: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.RLock()

    # -- 读 --

    def get(self, key: Any, default: Any = None) -> Any:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return default
            seen_at, value = entry
            if self._expired(seen_at):
                del self._data[key]
                return default
            return value

    def timestamp(self, key: Any) -> float | None:
        """取条目的**业务时间戳**；不存在或已过期返回 None。

        `SessionCooldown` 用它做冷却判定：值本身没有意义，时间戳才是。

        关键区分：条目内部同时记录两个时间——
        - ``seen_at``：本 map 的时钟（用于 TTL/淘汰），
        - ``value``：调用方写入的业务时间戳（冷却判定的依据）。

        重构过程中曾把两者合并成一个字段，结果 `mark(now=100.0)` 写入的
        业务时间戳被 map 自己的墙钟覆盖，注入合成时间的测试与调用方
        全部失效。现在两者独立存储。
        """
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            seen_at, value = entry
            if self._expired(seen_at):
                del self._data[key]
                return None
            return value if isinstance(value, (int, float)) else None

    def has(self, key: Any) -> bool:
        """条目是否存在且未过期（与值是否为 None 无关）。"""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return False
            seen_at, _ = entry
            if self._expired(seen_at):
                del self._data[key]
                return False
            return True

    def __contains__(self, key: Any) -> bool:
        return self.has(key)

    def __len__(self) -> int:
        with self._lock:
            self._purge_locked()
            return len(self._data)

    def items(self) -> Iterator[tuple[Any, Any]]:
        """遍历未过期条目（返回快照，避免迭代时持锁）。"""
        with self._lock:
            self._purge_locked()
            snapshot = [(k, v) for k, (_, v) in self._data.items()]
        return iter(snapshot)

    def keys(self) -> list[Any]:
        with self._lock:
            self._purge_locked()
            return list(self._data.keys())

    # -- 写 --

    def set(self, key: Any, value: Any) -> None:
        now = self._clock()
        with self._lock:
            self._data[key] = (now, value)
            if len(self._data) > self._max_size:
                self._evict_locked()

    def pop(self, key: Any, default: Any = None) -> Any:
        with self._lock:
            entry = self._data.pop(key, None)
            return entry[1] if entry is not None else default

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    # -- 内部 --

    def _expired(self, ts: float) -> bool:
        if self._ttl <= 0:
            return False
        return (self._clock() - ts) > self._ttl

    def _purge_locked(self) -> None:
        if self._ttl <= 0:
            return
        dead = [k for k, (ts, _) in self._data.items() if self._expired(ts)]
        for k in dead:
            del self._data[k]

    def _evict_locked(self) -> None:
        """超出容量：先清过期，仍超则淘汰最久未更新的条目。"""
        self._purge_locked()
        overflow = len(self._data) - self._max_size
        if overflow <= 0:
            return
        oldest = sorted(self._data.items(), key=lambda kv: kv[1][0])[:overflow]
        for k, _ in oldest:
            del self._data[k]


_SENTINEL = object()


class SessionCooldown:
    """按 session 的冷却记录基类。

    Phase 1 审计：`trigger.InterruptCooldown`、`image_gen.DrawCooldown`、
    `browser_agent.BrowseCooldown`、`group_card.CardCooldown`、
    `group_mute.MuteCooldown` 是**同一段代码的 5 份副本**，都是
    `dict[str, float]` + `ready()` + `mark()`，且都无界。

    这里统一成一个基类，语义与重构前逐字一致（含 `cooldown_sec <= 0`
    视为不限制、无记录视为 ready）。

    容量治理：只保留**最近 N 个 session**（LRU 淘汰），不设 TTL。

    为什么不用 TTL 而用容量上限：`ready()`/`mark()` 允许调用方注入
    `now`（测试用它验证"60 秒后解禁"）。若按真实墙钟做 TTL 过期，
    注入的合成时间戳（如 100.0）会被判为"远古记录"而清除，
    冷却语义随之失效。容量上限只约束内存占用，不参与正确性判断，
    因此对语义完全无影响。
    """

    max_sessions: int = 8192

    def __init__(self) -> None:
        # ttl=0 → 永不过期，仅受 max_size 约束
        self._last: BoundedTTLMap = BoundedTTLMap(
            ttl=0.0, max_size=self.max_sessions
        )

    def ready(self, session_id: str, cooldown_sec: int, now: float | None = None) -> bool:
        if cooldown_sec <= 0:
            return True
        last = self._last.timestamp(session_id)
        if last is None:
            return True
        now = time.time() if now is None else now
        return (now - last) >= cooldown_sec

    def mark(self, session_id: str, now: float | None = None) -> None:
        self._last.set(session_id, time.time() if now is None else now)

    def __len__(self) -> int:
        return len(self._last)


__all__ = ["BoundedTTLMap", "SessionCooldown"]
