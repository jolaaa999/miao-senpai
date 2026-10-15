"""内置 Retriever 实现：测试与默认用。

Phase 6B 第三十九节：**先实现 Null / Fake / InMemory，不要一上来接 RAGFlow。**

理由：先用假 provider 把架构跑通（Tool → Service → Retriever → ToolResult
→ Agent continuation），证明分层是对的，再换真 provider。
如果先接真 API，架构问题会被网络问题掩盖。

第五十二节：本阶段**禁止**自己实现 embedding / FAISS / Milvus / pgvector
—— 后续计划用 RAGFlow，不要重复造向量知识库。
因此这里的 InMemory 用**简单关键词匹配**，刻意不引入向量检索。
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

import httpx

from .models import (
    DatasetAccess,
    KnowledgeChunk,
    KnowledgeQuery,
    KnowledgeResult,
    KnowledgeScope,
    KnowledgeSource,
)


class FakeRetriever:
    """固定数据的假检索器（测试与演示用）。

    行为完全确定，便于断言：
        - 按预置的 `dataset` 过滤
        - 按预置的 `scope` 做隔离
        - 可选 `delay` 模拟慢 provider（测试 timeout）
        - 可选 `raise_error` 模拟 provider 故障
    """

    def __init__(
        self,
        *,
        name: str = "fake",
        chunks: list[KnowledgeChunk] | None = None,
        sources: list[KnowledgeSource] | None = None,
        delay: float = 0.0,
        fail: bool = False,
    ) -> None:
        self._name = name
        self._chunks = list(chunks or [])
        self._sources = list(sources or [])
        self._delay = delay
        self._fail = fail
        self.call_count = 0
        self.last_query: KnowledgeQuery | None = None

    @property
    def name(self) -> str:
        return self._name

    def list_sources(self) -> list[KnowledgeSource]:
        return list(self._sources)

    async def retrieve(self, query: KnowledgeQuery) -> KnowledgeResult:
        self.call_count += 1
        self.last_query = query
        started = time.perf_counter()

        if self._delay:
            await asyncio.sleep(self._delay)

        if self._fail:
            return KnowledgeResult(
                chunks=[], provider=self._name, ok=False,
                error="retriever_failure",
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        # 权限过滤：只返回当前请求方有权访问的来源下的 chunk
        #
        # 判定分三种情况（这是刻意的宽容策略）：
        #   1. chunk 没声明 source_id        → 放行（无来源信息，无从判定）
        #   2. source_id 在 self._sources 里 → 用 allows() 判定
        #   3. source_id 未登记              → 放行，交给 Service 的
        #      dataset 校验去把关；provider 可能持有 Service 未登记的来源
        #
        # 为什么不"未登记就拒绝"：那样 provider 必须先向 Service 注册
        # 全部来源才能工作，耦合过强。真正的越权拦截在
        # KnowledgeService._check_dataset_access（显式指定 dataset 时）。
        denied: set[str] = set()
        for s in self._sources:
            if not s.allows(user_id=query.user_id, group_id=query.group_id):
                denied.add(s.source_id)

        # 显式指定 dataset 时进一步收窄
        if query.dataset_ids:
            denied |= {
                c.source_id for c in self._chunks
                if c.source_id and c.source_id not in query.dataset_ids
            }

        picked = [
            c for c in self._chunks
            if (not c.source_id) or (c.source_id not in denied)
        ]
        # scope 收窄：GROUP/USER scope 只看对应归属
        if query.scope is KnowledgeScope.GROUP and query.group_id:
            picked = [
                c for c in picked
                if not c.metadata.get("group_id")
                or str(c.metadata.get("group_id")) == str(query.group_id)
            ]
        elif query.scope is KnowledgeScope.USER and query.user_id:
            picked = [
                c for c in picked
                if not c.metadata.get("user_id")
                or str(c.metadata.get("user_id")) == str(query.user_id)
            ]

        picked = sorted(picked, key=lambda c: c.score, reverse=True)[: query.top_k]
        return KnowledgeResult(
            chunks=picked,
            provider=self._name,
            duration_ms=(time.perf_counter() - started) * 1000,
            ok=True,
        )


class InMemoryRetriever:
    """基于关键词的内存检索器（第三十九节）。

    **刻意不做向量检索**（第五十二节）：本阶段的目的是证明
    "Knowledge 层可以换 provider 而 Agent 无感"，不是做检索质量。

    评分方式：query 词命中次数归一化。简单、可解释、零依赖。
    """

    def __init__(self, *, name: str = "memory") -> None:
        self._name = name
        self._chunks: list[KnowledgeChunk] = []
        self._sources: dict[str, KnowledgeSource] = {}

    @property
    def name(self) -> str:
        return self._name

    def add_source(self, source: KnowledgeSource) -> None:
        self._sources[source.source_id] = source

    def add(
        self,
        text: str,
        *,
        source_id: str = "",
        chunk_id: str = "",
        page: int | None = None,
        section: str = "",
        uri: str = "",
        source_name: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> KnowledgeChunk:
        chunk = KnowledgeChunk(
            text=text,
            chunk_id=chunk_id or f"c{len(self._chunks)}",
            source_id=source_id,
            source_name=source_name or self._sources.get(
                source_id, KnowledgeSource(source_id=source_id)
            ).name,
            page=page,
            section=section,
            uri=uri,
            metadata=dict(metadata or {}),
        )
        self._chunks.append(chunk)
        return chunk

    def list_sources(self) -> list[KnowledgeSource]:
        return list(self._sources.values())

    @staticmethod
    def _tokens(text: str) -> set[str]:
        """极简分词：按非字母数字切分 + 单字（中文无空格）。"""
        import re

        lowered = (text or "").lower()
        words = set(re.findall(r"[a-z0-9]+", lowered))
        words |= {ch for ch in lowered if "\u4e00" <= ch <= "\u9fff"}
        return words

    async def retrieve(self, query: KnowledgeQuery) -> KnowledgeResult:
        started = time.perf_counter()
        if query.is_empty:
            return KnowledgeResult(
                chunks=[], provider=self._name, ok=True,
                duration_ms=(time.perf_counter() - started) * 1000,
            )

        # 与 FakeRetriever 同一套宽容策略：未登记的 source 放行，
        # 已登记但无权访问的才拦。
        denied = {
            s.source_id for s in self._sources.values()
            if not s.allows(user_id=query.user_id, group_id=query.group_id)
        }
        if query.dataset_ids:
            denied |= {
                c.source_id for c in self._chunks
                if c.source_id and c.source_id not in query.dataset_ids
            }

        q_tokens = self._tokens(query.query)
        scored: list[KnowledgeChunk] = []
        for chunk in self._chunks:
            if chunk.source_id and chunk.source_id in denied:
                continue
            c_tokens = self._tokens(chunk.text)
            if not c_tokens or not q_tokens:
                continue
            overlap = len(q_tokens & c_tokens)
            if overlap == 0:
                continue
            score = overlap / max(1, len(q_tokens))
            scored.append(
                KnowledgeChunk(
                    text=chunk.text,
                    chunk_id=chunk.chunk_id,
                    score=round(score, 4),
                    source_id=chunk.source_id,
                    source_name=chunk.source_name,
                    page=chunk.page,
                    section=chunk.section,
                    uri=chunk.uri,
                    metadata=dict(chunk.metadata),
                )
            )

        scored.sort(key=lambda c: c.score, reverse=True)
        return KnowledgeResult(
            chunks=scored[: query.top_k],
            provider=self._name,
            duration_ms=(time.perf_counter() - started) * 1000,
            ok=True,
        )


class NotConfiguredError(RuntimeError):
    """provider 未配置（如未填 RAGFlow 地址/密钥）。

    历史说明：Phase 6B 时 `RagflowRetriever` 用它表达「只定义边界、未实现」。
    真实实现接入后，Retriever Protocol 要求 `retrieve` **永不抛异常**，
    因此内置检索器不再抛出它，改为返回
    `KnowledgeResult(ok=False, error="not_configured")`。
    保留这个类型是为了不破坏 `knowledge` 包的公开导出；自定义 provider 若
    想走「快速失败」语义仍可使用。
    """


# RAGFlow 的 document_keyword 是**原始文件名**，实测带一堆下载站标记。
# 直接塞进提示词会把 "z-library.sk, 1lib.sk" 这类噪音喂给模型，
# 甚至被原样引用给用户 —— 所以显示前要清洗。
_SOURCE_NOISE_TAGS = ("z-library", "1lib", "z-lib", "libgen", "annas-archive", "annas")
_ETC_RE = re.compile(r"\betc\.?\b", re.IGNORECASE)
_PAREN_RE = re.compile(r"[（(][^（()）]*[)）]")
_EXT_RE = re.compile(r"\.(pdf|epub|mobi|azw3|txt|docx?|pptx?|md)$", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def _clean_source_name(raw: str) -> str:
    """把原始文件名整理成可读的来源名（用于引用展示）。

    只做三件低风险的事：
        1. 丢掉含下载站标记的括号片段；
        2. 丢掉只写着 "etc." 的括号片段（Z-Library 元数据里的作者占位）；
        3. 丢掉扩展名、压缩多余空白。

    刻意**不做**「猜书名」这类激进处理：猜错会把引用变成错误信息，
    比留一点噪音更糟（第五十一节：宁可缺，不能编造）。
    """
    name = (raw or "").strip()
    if not name:
        return ""

    def _drop_paren(match: "re.Match[str]") -> str:
        body = match.group(0).lower()
        if any(tag in body for tag in _SOURCE_NOISE_TAGS):
            return ""
        if _ETC_RE.search(body):
            return ""
        return match.group(0)

    name = _PAREN_RE.sub(_drop_paren, name)
    name = _EXT_RE.sub("", name)
    return _WS_RE.sub(" ", name).strip()


def _as_float(value: Any, default: float = 0.0) -> float:
    """把 RAGFlow 返回值安全转成 float（它可能给字符串或 None）。"""
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


class RagflowRetriever:
    """RAGFlow 检索器 —— 真实 HTTP 实现（2026-10-11 接入）。

    第五十三节原本要求「只定义边界，不实现」，因为当时没有真实 API 环境。
    现在有了内网部署和可用 dataset，就按那一节预留的方式把实现补齐：
    **对外接口形状完全不变**，KnowledgeService / Knowledge Tool / Agent
    一行都不用改 —— 这正是当初分层想要的效果。

    实测的 API 形状：
        POST {base}/api/v1/retrieval   检索
        GET  {base}/api/v1/datasets    数据集列表

    其中 base **必须包含部署前缀**（如 http://host:port/ontoweb-ragflow）。
    少了前缀 nginx 直接 404 —— 这是实测踩到的坑，不要自作聪明裁剪路径。

    网络层：显式 trust_env=False。本机开着系统代理，httpx 默认会读环境代理
    并把发往内网 10.x 的请求也塞进去，表现为连接失败或 502。
    """

    def __init__(
        self,
        *,
        base_url: str = "",
        api_key: str = "",
        name: str = "ragflow",
        timeout: float = 15.0,
        similarity_threshold: float = 0.2,
        vector_weight: float = 0.3,
        default_dataset_ids: list[str] | None = None,
    ) -> None:
        self._name = name
        self._base_url = (base_url or "").rstrip("/")
        self._api_key = api_key or ""
        self._timeout = timeout
        self._similarity_threshold = similarity_threshold
        self._vector_weight = vector_weight
        # 配置里写死的 dataset（query 未指定时用）
        self._default_dataset_ids = list(default_dataset_ids or [])
        # retrieve() 顺带缓存，供同步的 list_sources() 使用
        self._source_cache: dict[str, KnowledgeSource] = {}
        # 从 /api/v1/datasets 自动发现的 dataset id（进程内缓存，避免每次检索都拉一遍）
        self._dataset_cache: list[str] = []

    @property
    def name(self) -> str:
        return self._name

    @property
    def configured(self) -> bool:
        return bool(self._base_url and self._api_key)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer " + self._api_key}

    def list_sources(self) -> list[KnowledgeSource]:
        """列出**已见过**的知识来源。

        为什么不是实时拉取：Protocol 把本方法定义为同步，而 dataset 列表是
        HTTP 接口。在同步方法里联网会阻塞事件循环，因此改为返回 retrieve()
        过程中缓存下来的来源；没检索过就返回空列表。
        宁可少报，也不在同步路径上偷偷发网络请求。
        """
        return list(self._source_cache.values())

    async def _discover_dataset_ids(self, client: Any) -> list[str]:
        """从 /api/v1/datasets 拉取当前 key 可见的 dataset id。

        为什么需要它：实测这台 RAGFlow 的 /retrieval **强制要求 dataset_ids**，
        不传直接返回 "`dataset_ids` is required."，并不存在「传空就全库检索」
        的默认行为。所以当配置和 query 都没给 dataset 时，必须先问服务端
        有哪些 dataset 可用。结果在进程内缓存，避免每次检索多一次往返。
        """
        if self._dataset_cache:
            return self._dataset_cache
        resp = await client.get(self._base_url + "/api/v1/datasets", headers=self._headers())
        if resp.status_code != 200:
            return []
        body = resp.json()
        items = body.get("data") or []
        ids = [str(it.get("id")) for it in items if isinstance(it, dict) and it.get("id")]
        if ids:
            self._dataset_cache = ids
        return ids

    def _to_chunk(self, raw: dict[str, Any]) -> KnowledgeChunk:
        """把一条 RAGFlow 命中映射成 KnowledgeChunk。"""
        doc_id = str(raw.get("document_id") or "")
        raw_name = str(raw.get("document_keyword") or raw.get("docnm_kwd") or "")
        doc_name = _clean_source_name(raw_name) or raw_name
        if doc_id and doc_id not in self._source_cache:
            self._source_cache[doc_id] = KnowledgeSource(
                source_id=doc_id,
                name=doc_name or doc_id,
            )
        return KnowledgeChunk(
            text=str(raw.get("content") or ""),
            chunk_id=str(raw.get("id") or ""),
            score=_as_float(raw.get("similarity")),
            source_id=doc_id,
            source_name=doc_name,
            # RAGFlow 的 positions 是版面坐标/偏移，并**不是页码**。
            # 拿不到真实页码就不填：引用宁可缺，也不能编造（第五十一节）。
            page=None,
            metadata={
                # 原始文件名一并保留：清洗过的名字好看，但排查问题时要能对上原文
                "raw_name": raw_name,
                "dataset_id": raw.get("dataset_id"),
                "vector_similarity": raw.get("vector_similarity"),
                "term_similarity": raw.get("term_similarity"),
            },
        )

    async def retrieve(self, query: KnowledgeQuery) -> KnowledgeResult:
        """执行检索。

        **任何失败都不抛异常**，一律返回 KnowledgeResult(ok=False, ...)。
        这既是 Retriever Protocol 的契约，也符合「知识库查不到不该让整条
        回复失败」的要求（第二十六节精神）。
        """
        started = time.perf_counter()

        def _elapsed() -> float:
            return (time.perf_counter() - started) * 1000.0

        if not self.configured:
            return KnowledgeResult(
                chunks=[],
                provider=self._name,
                ok=False,
                error="not_configured",
                duration_ms=_elapsed(),
            )

        top_k = max(1, min(int(query.top_k), 50))

        try:
            # 每次调用建一个 client：检索频率低，换来的是绝不会有连接泄漏，
            # 且不必管理长生命周期对象的关闭时机。
            async with httpx.AsyncClient(
                trust_env=False, timeout=self._timeout
            ) as client:
                # 本部署的 /retrieval 强制要求 dataset_ids（不传直接报错），
                # 所以按 query → 配置 → 自动发现 的顺序解析。
                dataset_ids = list(query.dataset_ids) or list(self._default_dataset_ids)
                if not dataset_ids:
                    dataset_ids = await self._discover_dataset_ids(client)
                if not dataset_ids:
                    return KnowledgeResult(
                        chunks=[],
                        provider=self._name,
                        ok=False,
                        error="no_dataset",
                        duration_ms=_elapsed(),
                    )
                resp = await client.post(
                    self._base_url + "/api/v1/retrieval",
                    headers=self._headers(),
                    json={
                        "question": query.query,
                        "dataset_ids": dataset_ids,
                        "top_k": top_k,
                        "similarity_threshold": self._similarity_threshold,
                        "vector_similarity_weight": self._vector_weight,
                    },
                )
            status = resp.status_code
            body = resp.json()
        except Exception as exc:  # noqa: BLE001 — 契约要求吞掉所有异常
            return KnowledgeResult(
                chunks=[],
                provider=self._name,
                ok=False,
                error=type(exc).__name__,
                duration_ms=_elapsed(),
            )

        if status != 200:
            return KnowledgeResult(
                chunks=[],
                provider=self._name,
                ok=False,
                error="http_" + str(status),
                duration_ms=_elapsed(),
            )

        # RAGFlow 用 code=0 表示成功；非 0 时 message 里带原因
        code = body.get("code")
        if code not in (0, None):
            return KnowledgeResult(
                chunks=[],
                provider=self._name,
                ok=False,
                error=str(body.get("message") or ("code_" + str(code))),
                duration_ms=_elapsed(),
            )

        data = body.get("data") or {}
        raw_chunks = data.get("chunks") or []
        chunks = [
            self._to_chunk(item) for item in raw_chunks if isinstance(item, dict)
        ]
        # 空文本命中没有价值，只会白占上下文预算
        chunks = [c for c in chunks if c.text.strip()]
        chunks.sort(key=lambda c: c.score, reverse=True)
        # 必须自己截断：实测服务端**忽略 top_k**，请求 2 条照样返回 30 条。
        # 不截断会把上下文预算瞬间撑爆（Protocol 明确要求尊重 top_k）。
        chunks = chunks[:top_k]

        return KnowledgeResult(
            chunks=chunks,
            provider=self._name,
            ok=True,
            duration_ms=_elapsed(),
            metadata={"total": data.get("total")},
        )

class DummyRetriever:
    """最小可用检索器 —— 用于验证"加 provider 不改 Agent"（第五十五节）。

    它只返回一条固定内容，存在的意义是扩展性测试：
    注册它之后，Agent / PromptBuilder / ToolExecutor **一行都不用改**。
    """

    def __init__(self, *, name: str = "dummy", text: str = "dummy knowledge") -> None:
        self._name = name
        self._text = text

    @property
    def name(self) -> str:
        return self._name

    def list_sources(self) -> list[KnowledgeSource]:
        return [KnowledgeSource(source_id="dummy", name="Dummy 知识库")]

    async def retrieve(self, query: KnowledgeQuery) -> KnowledgeResult:
        return KnowledgeResult(
            chunks=[
                KnowledgeChunk(
                    text=self._text,
                    chunk_id="dummy-0",
                    score=1.0,
                    source_id="dummy",
                    source_name="Dummy 知识库",
                )
            ],
            provider=self._name,
            ok=True,
        )


__all__ = [
    "FakeRetriever",
    "InMemoryRetriever",
    "DummyRetriever",
    "RagflowRetriever",
    "NotConfiguredError",
]
