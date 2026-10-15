"""Retriever Protocol 与注册表。

Phase 6B 第三十三/三十八/三十九节。

架构（严格分层，Agent 不认识任何具体 provider）：

    AgentRuntime
         |
    Knowledge Tool
         |
    KnowledgeService
         |
    RetrieverRegistry
       /        \\
  NullRetriever  Future RagflowRetriever

**为什么用 Protocol 而不是基类**：
Protocol 是结构化类型 —— 任何有 `async retrieve(query)` 的对象都能注册，
不需要继承我们的基类。这样将来 RAGFlow adapter 可以独立演化，
甚至由第三方 SDK 包装，不必迁就我们的类层次。
"""

from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

from .models import KnowledgeChunk, KnowledgeQuery, KnowledgeResult


@runtime_checkable
class Retriever(Protocol):
    """检索器统一接口（第三十八节）。

    实现者只需要满足这一个方法。
    """

    @property
    def name(self) -> str:
        """provider 名（用于观测与 list_sources）。"""
        ...

    async def retrieve(self, query: KnowledgeQuery) -> KnowledgeResult:
        """执行检索。

        约定：
            - **不得抛异常**，失败时返回 `KnowledgeResult(ok=False, error=...)`。
              （调用方另有兜底，但 provider 自己吞掉异常更可靠）
            - 结果按 score 降序
            - 必须尊重 `query.top_k`
        """
        ...

    def list_sources(self) -> list[Any]:
        """列出该 provider 拥有的知识来源。"""
        ...


class NullRetriever:
    """不做任何事、永远返回空的检索器（第三十九节）。

    用途：
        1. 默认 provider —— 没配置知识库时行为明确（"查不到"），
           而不是报错或静默走别的路径；
        2. 测试基准。
    """

    def __init__(self, name: str = "null") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def retrieve(self, query: KnowledgeQuery) -> KnowledgeResult:
        return KnowledgeResult(chunks=[], provider=self._name, ok=True)

    def list_sources(self) -> list[Any]:
        return []


class RetrieverRegistry:
    """检索器注册表（第三十三节）。

    设计目标（第五十五节）：新增一个 Retriever 只需要
    `registry.register(DummyRetriever())`，
    **不需要修改** AgentRuntime / PromptBuilder / ToolExecutor。
    """

    def __init__(self, default: str = "") -> None:
        self._retrievers: dict[str, Retriever] = {}
        self._default = default

    def register(self, retriever: Retriever, *, default: bool = False) -> None:
        """注册一个检索器。

        Args:
            retriever: 满足 Retriever Protocol 的对象。
            default: 是否设为默认 provider。
        """
        name = getattr(retriever, "name", "") or type(retriever).__name__
        self._retrievers[name] = retriever
        if default or not self._default:
            self._default = name

    def unregister(self, name: str) -> None:
        self._retrievers.pop(name, None)
        if self._default == name:
            self._default = next(iter(self._retrievers), "")

    def get(self, name: str = "") -> Retriever | None:
        """取检索器。name 为空时返回默认。"""
        if not name:
            name = self._default
        return self._retrievers.get(name)

    def names(self) -> list[str]:
        return sorted(self._retrievers)

    @property
    def default_name(self) -> str:
        return self._default

    def __len__(self) -> int:
        return len(self._retrievers)

    def __contains__(self, name: object) -> bool:
        return name in self._retrievers


class CompositeRetriever:
    """把多个 Retriever 的结果合成一份（混合检索）。

    为什么需要它：
        KnowledgeService 一次只解析出一个 provider（见 service._resolve），
        而现在要把「本地 Markdown 资料」和「RAGFlow」一起摆给模型看。
        与其改 Service 的语义，不如把"扇出 + 合并"做成一个普通 Retriever ——
        这正是注册表这套设计想支持的扩展方式（第五十五节）。

    合并为什么用轮转交替，而不是按 score 混排：
        不同 provider 的分数尺度根本不可比（RAGFlow 是余弦相似度，
        本地是词命中率）。直接混排会让某一方把另一方挤干净，
        结果就是"混合检索"名存实亡。轮转保证每个来源都被看到 ——
        与联网检索那边「多来源交叉印证」是同一个思路。

    容错：某个 provider 失败不影响其他 provider 的结果。
        知识库查不到不该让整条回复失败（第二十六节）。
    """

    def __init__(
        self,
        retrievers: list[Retriever] | None = None,
        *,
        name: str = "composite",
    ) -> None:
        self._retrievers: list[Retriever] = list(retrievers or [])
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    @property
    def members(self) -> list[Retriever]:
        return list(self._retrievers)

    def add(self, retriever: Retriever) -> None:
        self._retrievers.append(retriever)

    def list_sources(self) -> list[Any]:
        """汇总各成员的来源；单个成员失败只跳过它。"""
        out: list[Any] = []
        for r in self._retrievers:
            try:
                out.extend(r.list_sources() or [])
            except Exception:  # noqa: BLE001 — 成员自述失败不影响其他
                continue
        return out

    async def retrieve(self, query: KnowledgeQuery) -> KnowledgeResult:
        started = time.perf_counter()

        if not self._retrievers:
            return KnowledgeResult(
                chunks=[], provider=self._name, ok=True,
                duration_ms=(time.perf_counter() - started) * 1000.0,
            )

        results: list[KnowledgeResult] = []
        for r in self._retrievers:
            try:
                results.append(await r.retrieve(query))
            except Exception as exc:  # noqa: BLE001 — 一个库挂了不该拖垮另一个
                results.append(KnowledgeResult(
                    chunks=[], provider=getattr(r, "name", ""),
                    ok=False, error=type(exc).__name__,
                ))

        # 每个成员先各自按分数排好，再轮流取 —— 保证来源多样性
        buckets: list[list[KnowledgeChunk]] = []
        for res in results:
            chunks = [c for c in (res.chunks or []) if (c.text or "").strip()]
            chunks.sort(key=lambda c: c.score, reverse=True)
            if chunks:
                buckets.append(chunks)

        if not buckets:
            # 全都为空（或全失败）：只有全部失败才算失败
            all_failed = all(not res.ok for res in results)
            return KnowledgeResult(
                chunks=[], provider=self._name,
                duration_ms=(time.perf_counter() - started) * 1000.0,
                ok=not all_failed,
                error="all_failed" if all_failed else "",
                metadata={"providers": [res.provider for res in results]},
            )

        merged: list[KnowledgeChunk] = []
        seen: set[str] = set()
        limit = max(1, int(query.top_k))
        idx = 0
        while len(merged) < limit:
            progressed = False
            for bucket in buckets:
                if idx < len(bucket):
                    progressed = True
                    chunk = bucket[idx]
                    # 同一段内容被两个来源都返回时只留一份
                    key = (chunk.text or "")[:200]
                    if key not in seen:
                        seen.add(key)
                        merged.append(chunk)
                        if len(merged) >= limit:
                            break
            if not progressed:
                break
            idx += 1

        return KnowledgeResult(
            chunks=merged, provider=self._name,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            ok=any(res.ok for res in results),
            metadata={
                "providers": [res.provider for res in results],
                "per_provider": [len(b) for b in buckets],
            },
        )


# 全局注册表（生产用）
_registry: RetrieverRegistry | None = None


def get_retriever_registry() -> RetrieverRegistry:
    """取全局注册表。

    默认已注册 NullRetriever —— 保证"没配置知识库"时
    系统行为明确，而不是 None 崩溃。
    """
    global _registry
    if _registry is None:
        _registry = RetrieverRegistry()
        _registry.register(NullRetriever(), default=True)
    return _registry


def reset_retriever_registry() -> None:
    """重置（测试用）。"""
    global _registry
    _registry = None


__all__ = [
    "Retriever",
    "NullRetriever",
    "CompositeRetriever",
    "RetrieverRegistry",
    "get_retriever_registry",
    "reset_retriever_registry",
]
