"""服务层相关性闸门：库里没有这个内容时不许拿别的领域顶上。"""

from __future__ import annotations

import asyncio

from dl_senpai.knowledge.models import KnowledgeChunk, KnowledgeQuery, KnowledgeResult
from dl_senpai.knowledge.retriever import RetrieverRegistry
from dl_senpai.knowledge.service import KnowledgeService


class _StubRetriever:
    """固定返回给定切片，用来隔离测试闸门本身。"""

    name = "stub"

    def __init__(self, chunks: list[KnowledgeChunk]) -> None:
        self._chunks = chunks

    async def retrieve(self, query: KnowledgeQuery) -> KnowledgeResult:
        return KnowledgeResult(chunks=list(self._chunks), provider=self.name, ok=True)


def _service(chunks: list[KnowledgeChunk], *, gate: bool = True) -> KnowledgeService:
    registry = RetrieverRegistry()
    registry.register(_StubRetriever(chunks), default=True)
    return KnowledgeService(registry=registry, timeout=5.0, relevance_gate=gate)


def _run(chunks, query: str, *, gate: bool = True):
    return asyncio.run(_service(chunks, gate=gate).retrieve(KnowledgeQuery(query=query, top_k=5)))


def test_cross_domain_chunk_is_dropped() -> None:
    """问明日方舟，库里只有 C++ 书：标题完全没重合，必须判为「库里没有」。"""
    chunk = KnowledgeChunk(text="C++ 模板与泛型编程……", source_name="C++ Primer Plus：中文版（第六版）", score=0.352)
    result = _run([chunk], "明日方舟新手先练哪个干员")
    assert result.chunks == []
    assert result.metadata.get("relevance_gate") == "1->0"


def test_title_match_is_kept() -> None:
    """标题里出现了查询的实词，就不能误杀。"""
    chunk = KnowledgeChunk(text="先练能站场的……", source_name="明日方舟 / 百科站点.md", section="明日方舟 · 干员培养表", score=0.35)
    result = _run([chunk], "明日方舟新手先练哪个干员")
    assert result.chunks, "标题命中却被闸门误杀"


def test_high_confidence_score_passes_without_term_overlap() -> None:
    """高置信分数直接放行：防语义命中但标题没重词被误杀。"""
    chunk = KnowledgeChunk(text="今天的推荐是……", source_name="AI-Agents-in-Depth", score=0.86)
    result = _run([chunk], "今天晚饭吃什么好")
    assert result.chunks


def test_gate_off_keeps_old_behavior() -> None:
    """关掉闸门时退回原行为（保留兜底，便于灰度与回滚）。"""
    chunk = KnowledgeChunk(text="C++ 模板……", source_name="C++ Primer Plus", score=0.352)
    result = _run([chunk], "明日方舟新手先练哪个干员", gate=False)
    assert result.chunks


def test_fallback_to_text_when_no_citation() -> None:
    """切片连标题都没有时退回正文判断，否则没有元数据的库会被全部误杀。"""
    chunk = KnowledgeChunk(text="明日方舟新手先练哪个干员：先练能站场的。", source_name="", score=0.3)
    result = _run([chunk], "明日方舟新手先练哪个干员")
    assert result.chunks
