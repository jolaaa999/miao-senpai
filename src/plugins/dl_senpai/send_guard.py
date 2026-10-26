"""发送闸门：熔断器 + 频率上限。

搬自 ai-companion-kit 的 docs/完整教程 第十三章「防风控」。
那一章是原作者的踩坑记录，结论比代码本身值钱：

    桥接出现发送失败后每 15 秒重试一次，连续 23 分钟，
    14 次失败 vs 全天 18 次成功——**在腾讯眼里这就是标准的机器人。**

腾讯判断机器人最看的不是你打几个字，而是「失败之后还一直重试」。
所以这里有两道闸门，都在真正调用 send 之前生效：

1. **熔断器**：连续失败 N 次就停 M 秒。期间**连尝试都不发起**，
   不是「失败了再等」，而是「先别去骚扰服务器」。
2. **频率上限**：每分钟 / 每小时最多发多少条。

默认值（6 条/分钟、80 条/小时、连续 3 次失败停 180 秒）都是原项目
踩坑后调出来的。调高不会让她更活泼，只会让账号更快进风控——
这个项目的限流参数请不要为了「响应更快」而放大。
"""

from __future__ import annotations

import time
from typing import Any

try:
    from nonebot import logger
except Exception:  # noqa: BLE001 — 单测/独立脚本可能没有 nonebot

    class _Logger:
        def warning(self, msg: str) -> None:
            print(msg)

        def info(self, msg: str) -> None:
            print(msg)

    logger = _Logger()  # type: ignore[assignment]


class CircuitBreaker:
    """连续失败到阈值就打开，冷却期内一律拒绝。"""

    def __init__(self, *, threshold: int = 3, cooldown_sec: float = 180.0) -> None:
        self.threshold = max(1, int(threshold))
        self.cooldown_sec = max(0.0, float(cooldown_sec))
        self._fails = 0
        self._open_until = 0.0
        self._opened_count = 0

    def allow(self) -> tuple[bool, str]:
        remain = self._open_until - time.time()
        if remain > 0:
            return False, f"熔断中（还剩 {int(remain)}s）"
        return True, ""

    def on_success(self) -> None:
        self._fails = 0

    def on_failure(self) -> bool:
        """记一次失败。返回「本次是否刚刚打开了熔断」。"""
        self._fails += 1
        if self._fails >= self.threshold and self._open_until <= time.time():
            self._open_until = time.time() + self.cooldown_sec
            self._opened_count += 1
            logger.warning(
                f"dl_senpai send breaker OPEN: {self._fails} 次连续失败，"
                f"停 {int(self.cooldown_sec)}s 不再尝试"
            )
            return True
        return False

    def status(self) -> dict[str, Any]:
        remain = max(0.0, self._open_until - time.time())
        return {
            "open": remain > 0,
            "remain_sec": int(remain),
            "consecutive_fails": self._fails,
            "opened_count": self._opened_count,
        }

    def reset(self) -> None:
        self._fails = 0
        self._open_until = 0.0


class RateWindow:
    """滑动窗口计数：一分钟 / 一小时各留最近 N 秒的时间戳。"""

    def __init__(self, *, per_minute: int = 6, per_hour: int = 80) -> None:
        self.per_minute = max(0, int(per_minute))
        self.per_hour = max(0, int(per_hour))
        self._stamps: list[float] = []

    def _prune(self, now: float) -> None:
        # 只保留一小时内的：更早的既不影响分钟也不影响小时判断
        cutoff = now - 3600.0
        self._stamps = [t for t in self._stamps if t > cutoff]

    def allow(self, now: float | None = None) -> tuple[bool, str]:
        ts = time.time() if now is None else now
        self._prune(ts)
        if self.per_minute > 0:
            in_minute = sum(1 for t in self._stamps if t > ts - 60.0)
            if in_minute >= self.per_minute:
                return False, f"每分钟上限 {self.per_minute} 条已满"
        if self.per_hour > 0 and len(self._stamps) >= self.per_hour:
            return False, f"每小时上限 {self.per_hour} 条已满"
        return True, ""

    def record(self, now: float | None = None) -> None:
        self._stamps.append(time.time() if now is None else now)

    def status(self, now: float | None = None) -> dict[str, int]:
        ts = time.time() if now is None else now
        self._prune(ts)
        return {
            "last_minute": sum(1 for t in self._stamps if t > ts - 60.0),
            "last_hour": len(self._stamps),
            "per_minute": self.per_minute,
            "per_hour": self.per_hour,
        }

    def reset(self) -> None:
        self._stamps = []


class SendGuard:
    """熔断 + 限流的统一入口。

    每次判定都**现读配置**，所以 .env 改完之后不用重启就能生效
    （与项目里其它模块的热重载约定一致）。
    """

    def __init__(self, config: Any = None) -> None:
        self._config = config
        self.breaker = CircuitBreaker()
        self.rate = RateWindow()
        self._last_applied: tuple[int, int, int, float] | None = None

    def _cfg(self) -> Any:
        if self._config is not None:
            return self._config
        try:
            from .config import get_config

            return get_config()
        except Exception:  # noqa: BLE001 — 拿不到配置就用默认值，别把发送卡死
            return None

    def _sync_limits(self, cfg: Any) -> None:
        """把配置里的阈值同步到两个闸门上（变了才重建，避免每轮清空计数）。"""
        threshold = int(getattr(cfg, "send_breaker_fail_threshold", 3) or 3)
        cooldown = float(getattr(cfg, "send_breaker_cooldown_sec", 180) or 180)
        per_minute = int(getattr(cfg, "send_rate_per_minute", 6) or 0)
        per_hour = int(getattr(cfg, "send_rate_per_hour", 80) or 0)
        signature = (threshold, per_minute, per_hour, cooldown)
        if signature == self._last_applied:
            return

        self.breaker.threshold = max(1, threshold)
        self.breaker.cooldown_sec = max(0.0, cooldown)
        self.rate.per_minute = max(0, per_minute)
        self.rate.per_hour = max(0, per_hour)
        self._last_applied = signature

    def before_send(self) -> tuple[bool, str]:
        """发之前问一句。False 表示这一条不该发。"""
        cfg = self._cfg()
        enabled = True if cfg is None else bool(
            getattr(cfg, "send_breaker_enable", True)
        )
        if not enabled:
            return True, ""

        self._sync_limits(cfg)

        ok, reason = self.breaker.allow()
        if not ok:
            return False, reason

        ok, reason = self.rate.allow()
        if not ok:
            return False, reason

        return True, ""

    def on_success(self) -> None:
        self.breaker.on_success()
        self.rate.record()

    def on_failure(self) -> None:
        self.breaker.on_failure()

    def status(self) -> dict[str, Any]:
        return {"breaker": self.breaker.status(), "rate": self.rate.status()}


_guard: SendGuard | None = None


def get_send_guard(config: Any = None) -> SendGuard:
    global _guard
    if config is not None:
        return SendGuard(config)
    if _guard is None:
        _guard = SendGuard()
    return _guard


def reset_send_guard() -> None:
    """测试用：把单例清回初始状态。"""
    global _guard
    _guard = None
