"""轻量指标收集：先定接口与内存实现，暂不接 Prometheus。

Phase 1 审计结论：请求量、错误率、P99 延迟、token 消耗、tool 失败率、
队列长度**全部没有**。本阶段不引入 prometheus_client 依赖（用户明确要求
「不要引入大量无必要依赖」），而是先定义一个稳定的接口 + 线程安全的内存
实现，满足：

1. 运行时可查询（控制台后续可以暴露一个只读 HTTP 端点）；
2. 单测可断言（回归测试用）；
3. 未来换成 Prometheus / OTLP 时，只需新增一个实现，调用方不改。

刻意不做的事：不加标签组合爆炸防护（本项目标签维度只有 session/tool 名，
规模可控）；不做持久化（指标重启清零是正常语义）。
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _Counter:
    value: float = 0.0


@dataclass
class _Histogram:
    """固定窗口的滑动采样，用于算 P50/P95/P99。

    为什么不用全量样本：长跑进程下延迟样本会无限增长。保留最近 N 个样本
    已经足够反映当前水位，且内存有界。
    """

    max_samples: int = 2048
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=2048))
    count: int = 0
    total: float = 0.0

    def observe(self, value: float) -> None:
        self.samples.append(value)
        self.count += 1
        self.total += value

    def percentile(self, q: float) -> float | None:
        if not self.samples:
            return None
        ordered = sorted(self.samples)
        # 最近邻取整，避免 numpy 依赖
        idx = int(round((len(ordered) - 1) * q))
        idx = max(0, min(len(ordered) - 1, idx))
        return ordered[idx]


class Metrics:
    """把「一个 AI 服务需要观测什么」按维度归集。

    计数器命名遵循 `<域>.<动作>`，与事件命名一致，便于对照日志。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, _Counter] = {}
        self._histograms: dict[str, _Histogram] = {}
        # 队列长度属于「瞬时值」，用 gauge 语义记录最近一次观测
        self._gauges: dict[str, float] = {}

    # -- 计数器 --

    def incr(self, name: str, value: float = 1.0) -> None:
        with self._lock:
            counter = self._counters.get(name)
            if counter is None:
                counter = _Counter()
                self._counters[name] = counter
            counter.value += value

    def counter(self, name: str) -> float:
        with self._lock:
            counter = self._counters.get(name)
            return counter.value if counter is not None else 0.0

    # -- 直方图 --

    def observe(self, name: str, value: float) -> None:
        with self._lock:
            hist = self._histograms.get(name)
            if hist is None:
                hist = _Histogram()
                self._histograms[name] = hist
            hist.observe(value)

    def histogram(self, name: str) -> dict[str, Any]:
        with self._lock:
            hist = self._histograms.get(name)
            if hist is None:
                return {"count": 0, "avg": None, "p50": None, "p95": None, "p99": None}
            return {
                "count": hist.count,
                "avg": (hist.total / hist.count) if hist.count else None,
                "p50": hist.percentile(0.50),
                "p95": hist.percentile(0.95),
                "p99": hist.percentile(0.99),
            }

    # -- Gauge --

    def gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def read_gauge(self, name: str) -> float | None:
        with self._lock:
            return self._gauges.get(name)

    # -- 导出 --

    def snapshot(self) -> dict[str, Any]:
        """一次性导出全部指标，供控制台/诊断端点使用。"""
        with self._lock:
            counters = {k: v.value for k, v in self._counters.items()}
            gauges = dict(self._gauges)
            histograms = {
                k: {
                    "count": h.count,
                    "avg": (h.total / h.count) if h.count else None,
                    "p50": h.percentile(0.50),
                    "p95": h.percentile(0.95),
                    "p99": h.percentile(0.99),
                }
                for k, h in self._histograms.items()
            }
        return {"counters": counters, "gauges": gauges, "histograms": histograms}

    def reset(self) -> None:
        """清空全部指标。仅供测试使用。"""
        with self._lock:
            self._counters.clear()
            self._histograms.clear()
            self._gauges.clear()


# 全局单例：本阶段先全局共享，Phase 3 引入 Runtime 容器后再改为注入
metrics = Metrics()


__all__ = ["Metrics", "metrics"]
