"""KnowledgeService：知识检索的唯一入口。

Phase 6B 第三十三/三十七节。

     AgentRuntime
          |
     Knowledge Tool        ← 只认识 KnowledgeService
          |
     KnowledgeService      ← 权限、超时、观测、预算都在这里
          |
     RetrieverRegistry
        /        \\
  NullRetriever  RagflowRetriever(将来)

**为什么权限与预算放在 Service 而不是 Tool**：
    将来除了 Tool，可能还有别的调用方（定时任务、admin 面板）。
    把策略放在 Service，所有调用方自动共享同一套约束。
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import replace
from typing import Any

from ..observability.logging import log_event
from ..observability.metrics import metrics
from .models import (
    DatasetAccess,
    KnowledgeQuery,
    KnowledgeResult,
    KnowledgeScope,
    KnowledgeSource,
)
from .retriever import NullRetriever, Retriever, RetrieverRegistry, get_retriever_registry


# 查询侧「实词探针」与切片侧「自称内容」：
# 向量库的「我没有这个内容」是表达不出来的 —— 它永远返回 top-K，库里没有
# 明日方舟也会拿 C++ Primer 顶上（实测 0.352）。而分数分不开域内域外
#（域外 0.352 / 域内最低 0.380；vector_weight 压到 0 后域外 0.220 > 域内 0.170），
# 所以只能退回字面证据：切片的标题/章节里到底有没有出现你问的东西。
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_ALNUM_RE = re.compile(r"[a-z0-9_+]{2,}")


def _probe_terms(text: str) -> set[str]:
    """把查询拆成实词探针：中文 2~4 字 n-gram + 英文/数字词。"""
    lowered = (text or "").casefold()
    probes: set[str] = set(_ALNUM_RE.findall(lowered))
    for run in _CJK_RE.findall(lowered):
        for size in (2, 3, 4):
            for i in range(len(run) - size + 1):
                probes.add(run[i:i + size])
    return probes


def _chunk_haystack(chunk: Any) -> str:
    """切片「自称是什么内容」：标题 + 章节。"""
    parts = [
        getattr(chunk, "source_name", "") or "",
        getattr(chunk, "section", "") or "",
    ]
    return " ".join(p for p in parts if p).casefold()


class KnowledgeError(RuntimeError):
    """知识层错误基类。"""


class KnowledgePermissionError(KnowledgeError):
    """无权访问指定 dataset。"""


class KnowledgeService:
    """知识检索服务（第三十七节）。

    Args:
        registry: Retriever 注册表。默认用全局注册表（含 NullRetriever）。
        timeout: 单次检索超时（秒）。provider 卡住不能拖死整个回复。
        max_chunks / max_chars: context budget（第五十节）。
    """

    def __init__(
        self,
        *,
        registry: RetrieverRegistry | None = None,
        timeout: float = 8.0,
        max_chunks: int = 5,
        max_chars: int = 4000,
        relevance_gate: bool = True,
        high_confidence: float = 0.75,
    ) -> None:
        self._registry = registry or get_retriever_registry()
        self._timeout = timeout
        self._max_chunks = max_chunks
        self._max_chars = max_chars
        self._relevance_gate = relevance_gate
        self._high_confidence = high_confidence
        # 额外注册的 dataset 权限策略（第四十八节）
        self._extra_policies: list[KnowledgeSource] = []

    # -- 配置 --

    def register_source_policy(self, source: KnowledgeSource) -> None:
        """注册一个 dataset 的访问策略。"""
        self._extra_policies = [
            s for s in self._extra_policies if s.source_id != source.source_id
        ]
        self._extra_policies.append(source)

    @property
    def registry(self) -> RetrieverRegistry:
        return self._registry

    # -- 主入口 --

    async def retrieve(
        self,
        query: KnowledgeQuery,
        *,
        provider: str = "",
    ) -> KnowledgeResult:
        """执行检索。

        **所有失败路径都返回 KnowledgeResult(ok=False)，不抛异常** ——
        知识库查不到不应该让用户的整条回复失败（第二十六节的精神：
        媒体降级、工具失败都要保留降级路径）。
        唯一例外是权限拒绝，它会明确抛 `KnowledgePermissionError`
        （因为那是"不该查"，而不是"查不到"）。
        """
        if query.is_empty:
            return KnowledgeResult(chunks=[], provider="", ok=True)

        # 1) 权限：显式 dataset 必须逐个校验
        self._check_dataset_access(query)

        # 2) 选 provider
        retriever = self._resolve(query, provider)
        if retriever is None:
            log_event(
                "knowledge.no_provider",
                level="warning",
                requested=provider or self._registry.default_name,
            )
            return KnowledgeResult(
                chunks=[], provider="", ok=False, error="no_retriever"
            )

        # 3) 调用（带超时 + 异常兜底）
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(
                retriever.retrieve(query), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            duration = (time.perf_counter() - started) * 1000
            metrics.incr("knowledge.timeout")
            log_event(
                "knowledge.timeout",
                level="warning",
                provider=getattr(retriever, "name", ""),
                timeout=self._timeout,
                **query.as_log_fields(),
            )
            return KnowledgeResult(
                chunks=[], provider=getattr(retriever, "name", ""),
                duration_ms=duration, ok=False, error="timeout",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — provider 故障不能拖垮回复
            duration = (time.perf_counter() - started) * 1000
            metrics.incr("knowledge.error")
            log_event(
                "knowledge.error",
                level="warning",
                provider=getattr(retriever, "name", ""),
                error=f"{type(exc).__name__}: {str(exc)[:160]}",
                **query.as_log_fields(),
            )
            return KnowledgeResult(
                chunks=[], provider=getattr(retriever, "name", ""),
                duration_ms=duration, ok=False,
                error=f"{type(exc).__name__}",
            )

        # 4) 相关性闸门：库里没有这个内容时，向量库照样给你 5 条最不差的。
        #    判据只能是字面证据（标题/章节里出现查询实词），分数靠不住：
        #    域外 0.352 / 域内最低 0.380，压 vector_weight 后域外反而更高。
        #    高置信分数（默认 0.75）直接放行，避免语义命中但标题无重词被误杀。
        if self._relevance_gate and result.chunks:
            probes = _probe_terms(query.query)
            if probes:
                haystacks = [_chunk_haystack(c) for c in result.chunks]
                # 有标题的库只信标题：正文里什么词都可能有（「今天」「什么」），
                # 拿正文判相关性会把「晚饭吃什么」这种问题喂进一堆书里。
                has_titles = any(h.strip() for h in haystacks)
                kept = []
                for chunk, hay in zip(result.chunks, haystacks):
                    if float(getattr(chunk, "score", 0.0) or 0.0) >= self._high_confidence:
                        kept.append(chunk)
                        continue
                    surface = hay if has_titles else (getattr(chunk, "text", "") or "")[:400].casefold()
                    if any(p in surface for p in probes):
                        kept.append(chunk)
                if len(kept) != len(result.chunks):
                    meta = dict(result.metadata)
                    meta["relevance_gate"] = "%d->%d" % (len(result.chunks), len(kept))
                    result = replace(result, chunks=kept, metadata=meta)

        # 4) 预算裁剪（第五十节）
        budgeted = result.apply_budget(
            max_chunks=min(self._max_chunks, query.top_k or self._max_chunks),
            max_chars=self._max_chars,
        )

        # 5) 观测（第五十节：不记录完整文档内容 / 完整 query）
        metrics.incr("knowledge.retrieve")
        metrics.incr("knowledge.chunks", float(len(budgeted.chunks)))
        metrics.observe("knowledge.latency_ms", budgeted.duration_ms)
        log_event(
            "knowledge.retrieve",
            success=budgeted.ok,
            **query.as_log_fields(),
            **budgeted.as_log_fields(),
        )
        return budgeted

    async def health(self) -> dict[str, Any]:
        """健康检查（第三十七节）。

        对每个已注册 provider 做一次空查询 —— 这是最轻的探活方式，
        不引入额外的 health 接口约定。
        """
        out: dict[str, Any] = {
            "providers": self._registry.names(),
            "default": self._registry.default_name,
            "ok": True,
        }
        details: list[dict[str, Any]] = []
        for name in self._registry.names():
            retriever = self._registry.get(name)
            if retriever is None:
                continue
            probe = KnowledgeQuery(query="health", top_k=1)
            started = time.perf_counter()
            try:
                await asyncio.wait_for(
                    retriever.retrieve(probe), timeout=min(self._timeout, 3.0)
                )
                details.append({"name": name, "ok": True})
            except Exception as exc:  # noqa: BLE001
                out["ok"] = False
                details.append(
                    {"name": name, "ok": False, "error": type(exc).__name__}
                )
            finally:
                details[-1]["duration_ms"] = round(
                    (time.perf_counter() - started) * 1000, 2
                )
        out["details"] = details
        return out

    def list_sources(self) -> list[KnowledgeSource]:
        """列出所有 provider 的知识来源（第三十七节）。

        合并 provider 自报的来源与显式注册的策略，
        后者用于"来源只有策略、没有 provider 自述"的情况。
        """
        seen: dict[str, KnowledgeSource] = {}
        for name in self._registry.names():
            retriever = self._registry.get(name)
            if retriever is None:
                continue
            try:
                for source in retriever.list_sources() or []:
                    seen.setdefault(source.source_id, source)
            except Exception:  # noqa: BLE001 — provider 自述失败不影响其他
                continue
        for source in self._extra_policies:
            seen.setdefault(source.source_id, source)
        return list(seen.values())

    # -- 内部 --

    def _resolve(self, query: KnowledgeQuery, provider: str) -> Retriever | None:
        retriever = self._registry.get(provider)
        if retriever is not None:
            return retriever
        # 显式指定 provider 但不存在 → 不回退到默认（避免静默换库）
        if provider:
            return None
        return self._registry.get("") or NullRetriever()

    def _check_dataset_access(self, query: KnowledgeQuery) -> None:
        """显式指定 dataset 时逐个校验权限（第四十七/四十八节）。

        关键场景：群 A 的私有知识库不能被群 B 调用。
        """
        if not query.dataset_ids:
            return
        known = {s.source_id: s for s in self.list_sources()}
        for dataset_id in query.dataset_ids:
            source = known.get(dataset_id)
            if source is None:
                # 未知 dataset：本阶段保守处理 —— 交给 provider 自己过滤。
                # 不在这里拒绝，因为 provider 可能持有 Service 未登记的来源。
                continue
            if not source.allows(user_id=query.user_id, group_id=query.group_id):
                metrics.incr("knowledge.permission_denied")
                log_event(
                    "knowledge.permission_denied",
                    level="warning",
                    dataset=dataset_id,
                    scope=source.scope.value,
                    access=source.access.value,
                )
                raise KnowledgePermissionError(
                    f"无权访问 dataset {dataset_id}"
                )

    # -- 将来扩展（当前不实现写入，第三十七节）--

    async def index(self, *args: Any, **kwargs: Any) -> None:
        """写入接口 —— **本阶段不实现**。

        Phase 6B 只做检索；索引/写入留给接入 RAGFlow 时实现。
        显式抛错而不是静默 pass，避免调用方误以为写成功了。
        """
        raise NotImplementedError("Knowledge 写入接口将在接入 RAGFlow 时实现")

    async def delete(self, *args: Any, **kwargs: Any) -> None:
        """删除接口 —— **本阶段不实现**。"""
        raise NotImplementedError("Knowledge 删除接口将在接入 RAGFlow 时实现")


__all__ = [
    "KnowledgeService",
    "KnowledgeError",
    "KnowledgePermissionError",
]
