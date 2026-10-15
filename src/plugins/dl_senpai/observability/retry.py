"""重试策略：把「等多久、试几次」从 LLM 实现里抽出来。

Phase 1 审计发现：`llm.py::_complete_once()` 对瞬时错误固定 sleep 60 秒、
重试 4 次。后果有两个：

1. 群聊体验：中转站 502 时机器人要等 **3 分钟**才回一句"稍等"，
   用户感知是"机器人死了"；
2. 单测：`test_llm.py` 的失败用例真的 sleep 了 180 秒，整个测试套件跑了
   **35 分钟**。测试本应是秒级的。

本模块既做抽取与注入，也承载生产默认值：瞬时错误已由早期「固定 60s、
重试 4 次」改为指数退避（2s/4s/8s，封顶 10s），把最坏等待从 3 分钟
压到约 14 秒。测试仍可注入零等待策略。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class RetryPolicy:
    """退避重试参数。

    Attributes:
        max_attempts: 总尝试次数（含首次），必须 >= 1。
        transient_base_delay: 瞬时错误（限流/5xx/超时）指数退避的基数秒数。
        transient_max_delay: 瞬时错误退避上限。
        unexpected_base_delay: 非预期异常的指数退避基数。
        unexpected_max_delay: 指数退避上限。
        retry_transient: 瞬时错误是否重试。
        retry_unexpected: 非预期异常是否重试。
    """

    max_attempts: int = 4
    transient_base_delay: float = 2.0
    transient_max_delay: float = 10.0
    unexpected_base_delay: float = 0.5
    unexpected_max_delay: float = 4.0
    retry_transient: bool = True
    retry_unexpected: bool = True

    def delay_for_transient(self, attempt: int) -> float:
        """瞬时错误等待时间：指数退避 2s, 4s, 8s（封顶 10s）。

        原先固定 60s × 4 次尝试，上游持续 5xx 时用户要等满 3 分钟才收到
        一句「稍等」，感知上等同于机器人死了。改成指数退避后总等待从
        180s 压到约 14s，仍保留 4 次尝试的容错。
        """
        return min(
            self.transient_max_delay,
            self.transient_base_delay * (2**attempt),
        )

    def delay_for_unexpected(self, attempt: int) -> float:
        """非预期异常的指数退避：0.5s, 1s, 2s, 4s（封顶）。"""
        return min(self.unexpected_max_delay, self.unexpected_base_delay * (2**attempt))

    def should_retry_transient(self, attempt: int) -> bool:
        if not self.retry_transient:
            return False
        return attempt < self.max_attempts - 1

    def should_retry_unexpected(self, attempt: int) -> bool:
        if not self.retry_unexpected:
            return False
        return attempt < self.max_attempts - 1

    async def sleep(self, seconds: float) -> None:
        """可被覆盖的等待实现。

        抽成方法而不是直接 ``asyncio.sleep``，是为了让测试能注入
        ``NoWaitRetryPolicy`` 或直接 monkeypatch 这一个点。
        """
        if seconds > 0:
            await asyncio.sleep(seconds)


class NoWaitRetryPolicy(RetryPolicy):
    """零等待策略：测试环境专用。

    保持重试次数语义（仍然会重试到 max_attempts），但不消耗真实时间。
    这样「503 重试后给出友好话术」这类行为仍然被真实覆盖，只是不用等。
    """

    def __init__(self, max_attempts: int = 4) -> None:
        super().__init__(
            max_attempts=max_attempts,
            transient_base_delay=0.0,
            transient_max_delay=0.0,
            unexpected_base_delay=0.0,
            unexpected_max_delay=0.0,
        )


# 生产默认：4 次尝试，瞬时错误指数退避（2s/4s/8s，封顶 10s）
DEFAULT_RETRY_POLICY = RetryPolicy()


__all__ = ["RetryPolicy", "NoWaitRetryPolicy", "DEFAULT_RETRY_POLICY"]
