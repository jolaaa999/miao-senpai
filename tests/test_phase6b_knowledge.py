"""Phase 6B：Knowledge Layer 测试。

覆盖第五十四节要求的全部场景：
    KnowledgeQuery validation
    scope isolation
    group A cannot retrieve group B dataset
    FakeRetriever retrieval
    empty result
    retriever timeout
    retriever exception
    Knowledge Tool → Agent continuation
    KnowledgeResult context truncation
    citation metadata preservation
    Permission deny
    Tool disabled
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from dl_senpai.knowledge import (
    DatasetAccess,
    DummyRetriever,
    FakeRetriever,
    InMemoryRetriever,
    KnowledgeChunk,
    KnowledgePermissionError,
    KnowledgeQuery,
    KnowledgeResult,
    KnowledgeScope,
    KnowledgeService,
    KnowledgeSource,
    NullRetriever,
    RagflowRetriever,
    RetrieverRegistry,
)


# ================================================================
# 1. KnowledgeQuery validation
# ================================================================


def test_query_defaults() -> None:
    q = KnowledgeQuery(query="死锁")
    assert q.scope is KnowledgeScope.GLOBAL
    assert q.top_k == 5
    assert q.dataset_ids == []


def test_query_top_k_is_bounded() -> None:
    """top_k 必须有界，否则 provider 可能被要求返回上万条。"""
    assert KnowledgeQuery(query="x", top_k=0).top_k == 5
    assert KnowledgeQuery(query="x", top_k=-3).top_k == 5
    assert KnowledgeQuery(query="x", top_k=9999).top_k == 50
    assert KnowledgeQuery(query="x", top_k=10).top_k == 10


def test_query_is_empty() -> None:
    assert KnowledgeQuery(query="").is_empty is True
    assert KnowledgeQuery(query="   ").is_empty is True
    assert KnowledgeQuery(query="死锁").is_empty is False


def test_query_hash_does_not_expose_text() -> None:
    """观测字段不能泄漏 query 原文（第四十九节）。"""
    q = KnowledgeQuery(query="我的身份证号是123456")
    fields = q.as_log_fields()
    assert "query" not in fields
    assert "身份证" not in str(fields)
    assert fields["query_hash"]
    assert fields["query_len"] == len("我的身份证号是123456")


def test_query_hash_is_stable() -> None:
    a = KnowledgeQuery(query="死锁").query_hash()
    b = KnowledgeQuery(query="死锁").query_hash()
    c = KnowledgeQuery(query="活锁").query_hash()
    assert a == b
    assert a != c


# ================================================================
# 2. Scope isolation
# ================================================================


def make_service_with_sources() -> tuple[KnowledgeService, FakeRetriever]:
    """构造带多来源的测试环境。"""
    sources = [
        KnowledgeSource(source_id="public", name="公共库",
                        access=DatasetAccess.PUBLIC),
        KnowledgeSource(source_id="gA", name="A群资料", scope=KnowledgeScope.GROUP,
                        access=DatasetAccess.GROUP, owner_id="100"),
        KnowledgeSource(source_id="gB", name="B群资料", scope=KnowledgeScope.GROUP,
                        access=DatasetAccess.GROUP, owner_id="200"),
        KnowledgeSource(source_id="u1", name="用户1私有", scope=KnowledgeScope.USER,
                        access=DatasetAccess.USER, owner_id="1"),
    ]
    chunks = [
        KnowledgeChunk(text="公共：操作系统概述", source_id="public", score=0.9),
        KnowledgeChunk(text="A群：死锁四条件", source_id="gA", score=0.9),
        KnowledgeChunk(text="B群：死锁四条件", source_id="gB", score=0.9),
        KnowledgeChunk(text="用户1：我的笔记", source_id="u1", score=0.9),
    ]
    retriever = FakeRetriever(chunks=chunks, sources=sources)
    registry = RetrieverRegistry()
    registry.register(retriever, default=True)
    return KnowledgeService(registry=registry), retriever


@pytest.mark.asyncio
async def test_group_a_cannot_retrieve_group_b_dataset() -> None:
    """核心隔离：群 A 请求 B 群的私有 dataset 必须被拒绝。"""
    service, _ = make_service_with_sources()
    query = KnowledgeQuery(
        query="死锁", scope=KnowledgeScope.GROUP,
        user_id="1", group_id="100", dataset_ids=["gB"],
    )
    with pytest.raises(KnowledgePermissionError):
        await service.retrieve(query)


@pytest.mark.asyncio
async def test_group_a_can_retrieve_its_own_dataset() -> None:
    service, _ = make_service_with_sources()
    query = KnowledgeQuery(
        query="死锁", scope=KnowledgeScope.GROUP,
        user_id="1", group_id="100", dataset_ids=["gA"],
    )
    result = await service.retrieve(query)
    assert result.ok
    assert all(c.source_id == "gA" for c in result.chunks)


@pytest.mark.asyncio
async def test_user_cannot_retrieve_other_user_dataset() -> None:
    service, _ = make_service_with_sources()
    query = KnowledgeQuery(
        query="笔记", scope=KnowledgeScope.USER,
        user_id="2", dataset_ids=["u1"],
    )
    with pytest.raises(KnowledgePermissionError):
        await service.retrieve(query)


@pytest.mark.asyncio
async def test_public_dataset_open_to_all() -> None:
    service, _ = make_service_with_sources()
    query = KnowledgeQuery(query="操作系统", dataset_ids=["public"])
    result = await service.retrieve(query)
    assert result.ok
    assert result.chunks


@pytest.mark.asyncio
async def test_group_scope_filters_chunks_by_group() -> None:
    """GROUP scope 下不能看到别的群的 chunk。"""
    service, _ = make_service_with_sources()
    query = KnowledgeQuery(
        query="死锁", scope=KnowledgeScope.GROUP, user_id="1", group_id="100",
    )
    result = await service.retrieve(query)
    assert all(c.source_id != "gB" for c in result.chunks)


@pytest.mark.asyncio
async def test_admin_dataset_denied_by_default() -> None:
    """ADMIN 级 dataset 本阶段保守拒绝（无管理员身份通道）。"""
    source = KnowledgeSource(
        source_id="adm", name="管理库", access=DatasetAccess.ADMIN
    )
    assert source.allows(user_id="1", group_id="100") is False


# ================================================================
# 3. FakeRetriever 基本检索
# ================================================================


@pytest.mark.asyncio
async def test_fake_retriever_returns_chunks() -> None:
    retriever = FakeRetriever(
        chunks=[KnowledgeChunk(text="内容", score=0.5, source_id="s")]
    )
    result = await retriever.retrieve(KnowledgeQuery(query="x"))
    assert result.ok
    assert len(result.chunks) == 1
    assert result.provider == "fake"


@pytest.mark.asyncio
async def test_fake_retriever_respects_top_k() -> None:
    chunks = [KnowledgeChunk(text=f"c{i}", score=i / 10) for i in range(10)]
    retriever = FakeRetriever(chunks=chunks)
    result = await retriever.retrieve(KnowledgeQuery(query="x", top_k=3))
    assert len(result.chunks) == 3
    # 必须按 score 降序（保高分）
    assert result.chunks[0].score >= result.chunks[-1].score


@pytest.mark.asyncio
async def test_fake_retriever_records_call() -> None:
    retriever = FakeRetriever()
    await retriever.retrieve(KnowledgeQuery(query="a"))
    await retriever.retrieve(KnowledgeQuery(query="b"))
    assert retriever.call_count == 2
    assert retriever.last_query.query == "b"


# ================================================================
# 4. 空结果
# ================================================================


@pytest.mark.asyncio
async def test_empty_result_is_not_failure() -> None:
    """查不到 ≠ 失败 —— 模型应该说"不知道"，不是收到 error。"""
    retriever = FakeRetriever(chunks=[])
    registry = RetrieverRegistry()
    registry.register(retriever, default=True)
    service = KnowledgeService(registry=registry)

    result = await service.retrieve(KnowledgeQuery(query="不存在的东西"))
    assert result.ok is True
    assert result.is_empty is True
    assert result.error == ""


@pytest.mark.asyncio
async def test_empty_query_short_circuits() -> None:
    retriever = FakeRetriever()
    registry = RetrieverRegistry()
    registry.register(retriever, default=True)
    service = KnowledgeService(registry=registry)

    result = await service.retrieve(KnowledgeQuery(query="   "))
    assert result.ok
    assert retriever.call_count == 0, "空 query 不该打到 provider"


def test_null_retriever_returns_empty() -> None:
    retriever = NullRetriever()
    result = asyncio.run(retriever.retrieve(KnowledgeQuery(query="x")))
    assert result.ok
    assert result.is_empty


# ================================================================
# 5. Retriever timeout
# ================================================================


@pytest.mark.asyncio
async def test_retriever_timeout_returns_failure() -> None:
    """provider 卡住不能拖死整条回复。"""
    retriever = FakeRetriever(delay=5.0, chunks=[KnowledgeChunk(text="x")])
    registry = RetrieverRegistry()
    registry.register(retriever, default=True)
    service = KnowledgeService(registry=registry, timeout=0.05)

    result = await service.retrieve(KnowledgeQuery(query="慢查询"))
    assert result.ok is False
    assert result.error == "timeout"
    assert result.chunks == []


@pytest.mark.asyncio
async def test_timeout_does_not_raise() -> None:
    """超时必须被吞成 KnowledgeResult，不能抛给调用方。"""
    retriever = FakeRetriever(delay=5.0)
    registry = RetrieverRegistry()
    registry.register(retriever, default=True)
    service = KnowledgeService(registry=registry, timeout=0.05)
    # 不抛异常即通过
    await service.retrieve(KnowledgeQuery(query="x"))


# ================================================================
# 6. Retriever exception
# ================================================================


@pytest.mark.asyncio
async def test_retriever_exception_returns_failure() -> None:
    retriever = FakeRetriever(fail=True)
    registry = RetrieverRegistry()
    registry.register(retriever, default=True)
    service = KnowledgeService(registry=registry)

    result = await service.retrieve(KnowledgeQuery(query="x"))
    assert result.ok is False
    assert result.error == "retriever_failure"


@pytest.mark.asyncio
async def test_retriever_raising_exception_is_caught() -> None:
    """provider 直接抛异常也要被兜住。"""

    class Exploding:
        name = "boom"

        async def retrieve(self, query: KnowledgeQuery) -> KnowledgeResult:
            raise ValueError("provider 内部炸了")

        def list_sources(self) -> list[Any]:
            return []

    registry = RetrieverRegistry()
    registry.register(Exploding(), default=True)
    service = KnowledgeService(registry=registry)

    result = await service.retrieve(KnowledgeQuery(query="x"))
    assert result.ok is False
    assert "ValueError" in result.error


@pytest.mark.asyncio
async def test_no_provider_returns_failure() -> None:
    """显式指定不存在的 provider 不该静默回退到默认库。"""
    service = KnowledgeService(registry=RetrieverRegistry())
    result = await service.retrieve(
        KnowledgeQuery(query="x"), provider="not-exist"
    )
    assert result.ok is False
    assert result.error == "no_retriever"


# ================================================================
# 7. Context truncation
# ================================================================


def test_budget_limits_chunk_count() -> None:
    result = KnowledgeResult(
        chunks=[KnowledgeChunk(text=f"c{i}", score=i / 10) for i in range(20)]
    )
    budgeted = result.apply_budget(max_chunks=3, max_chars=1000)
    assert len(budgeted.chunks) == 3


def test_budget_keeps_highest_scores() -> None:
    """裁剪必须保高分，而不是保前面几条。"""
    chunks = [
        KnowledgeChunk(text=f"low{i}", score=0.1) for i in range(5)
    ] + [KnowledgeChunk(text="high", score=0.99)]
    budgeted = KnowledgeResult(chunks=chunks).apply_budget(max_chunks=1)
    assert budgeted.chunks[0].text == "high"


def test_budget_truncates_long_chunk() -> None:
    result = KnowledgeResult(
        chunks=[KnowledgeChunk(text="x" * 5000, score=1.0)]
    )
    budgeted = result.apply_budget(max_chunks=1, max_chars=500)
    assert len(budgeted.chunks[0].text) <= 501  # 含省略号


def test_budget_marks_metadata() -> None:
    result = KnowledgeResult(chunks=[KnowledgeChunk(text="a", score=1.0)])
    budgeted = result.apply_budget(max_chunks=1, max_chars=1000)
    assert budgeted.metadata.get("budget_applied") is True


def test_budget_on_empty_result() -> None:
    budgeted = KnowledgeResult().apply_budget()
    assert budgeted.is_empty
    assert budgeted.ok


def test_budget_respects_minimum_per_chunk() -> None:
    """每 chunk 至少留 200 字符，否则短预算会拿到空文本。"""
    result = KnowledgeResult(
        chunks=[KnowledgeChunk(text="y" * 1000, score=1.0) for _ in range(10)]
    )
    budgeted = result.apply_budget(max_chunks=10, max_chars=100)
    assert len(budgeted.chunks[0].text) >= 200


# ================================================================
# 8. Citation metadata
# ================================================================


def test_citation_fields_present() -> None:
    """第五十一节：字段现在就预留，即使 provider 暂时填不满。"""
    chunk = KnowledgeChunk(text="x")
    assert chunk.source_id == ""
    assert chunk.source_name == ""
    assert chunk.page is None
    assert chunk.section == ""
    assert chunk.uri == ""


def test_citation_preserved_through_budget() -> None:
    """裁剪不能丢失来源信息，否则模型无法标注出处。"""
    chunk = KnowledgeChunk(
        text="z" * 3000, source_id="s1", source_name="操作系统讲义",
        page=42, section="第三章", uri="https://example.com/os.pdf", score=1.0,
    )
    budgeted = KnowledgeResult(chunks=[chunk]).apply_budget(
        max_chunks=1, max_chars=300
    )
    kept = budgeted.chunks[0]
    assert kept.source_id == "s1"
    assert kept.source_name == "操作系统讲义"
    assert kept.page == 42
    assert kept.section == "第三章"
    assert kept.uri == "https://example.com/os.pdf"


def test_citation_text_rendering() -> None:
    chunk = KnowledgeChunk(
        text="x", source_name="操作系统讲义", page=42, section="第三章"
    )
    text = chunk.citation_text()
    assert "操作系统讲义" in text
    assert "第三章" in text
    assert "42" in text


def test_has_citation() -> None:
    assert KnowledgeChunk(text="x").has_citation is False
    assert KnowledgeChunk(text="x", source_name="A").has_citation is True
    assert KnowledgeChunk(text="x", page=1).has_citation is True
    assert KnowledgeChunk(text="x", uri="http://a").has_citation is True


def test_prompt_text_includes_citation() -> None:
    result = KnowledgeResult(
        chunks=[
            KnowledgeChunk(text="死锁需要四个条件", source_name="讲义", page=7,
                           score=1.0)
        ]
    )
    text = result.to_prompt_text()
    assert "死锁需要四个条件" in text
    assert "讲义" in text
    assert "7" in text


def test_prompt_text_on_empty() -> None:
    text = KnowledgeResult().to_prompt_text()
    assert "没有检索到" in text


# ================================================================
# 9. KnowledgeManager / Tool
# ================================================================


def test_knowledge_tool_definition() -> None:
    """第四十二节：工具声明的完整性。"""
    from dl_senpai.agent.tools.knowledge_tool import build_knowledge_tool
    from dl_senpai.agent.tools.models import Continuation
    from dl_senpai.agent.tools.registry import FailurePolicy

    tool = build_knowledge_tool()
    assert tool.name == "knowledge"
    assert tool.marker == "KNOWLEDGE"
    assert tool.description
    assert tool.prompt_text
    assert tool.parameter_schema
    assert tool.permission == "knowledge.retrieve"
    assert tool.timeout and tool.timeout > 0
    assert tool.continuation is Continuation.RETURN_TO_MODEL
    assert tool.failure_policy is FailurePolicy.RETURN_TO_MODEL


def test_knowledge_tool_parsing() -> None:
    from dl_senpai.agent.tools.knowledge_tool import _parse_knowledge

    text, calls = _parse_knowledge("我查一下\n<<<KNOWLEDGE 死锁 四个条件>>>")
    assert len(calls) == 1
    assert calls[0].tool_name == "knowledge"
    assert calls[0].arguments["query"] == "死锁 四个条件"
    assert "KNOWLEDGE" not in text


def test_knowledge_tool_parsing_empty_query_ignored() -> None:
    from dl_senpai.agent.tools.knowledge_tool import _parse_knowledge

    _, calls = _parse_knowledge("<<<KNOWLEDGE>>>")
    assert calls == [], "空关键词不该触发检索"


def test_knowledge_tool_unclosed_marker_stripped() -> None:
    """未闭合 marker 必须剥离，不能泄漏给群友。"""
    from dl_senpai.agent.tools.knowledge_tool import _parse_knowledge

    text, calls = _parse_knowledge("查一下 <<<KNOWLEDGE 死锁")
    assert calls == []
    assert "KNOWLEDGE" not in text


def test_knowledge_tool_no_keyword_hardcoding() -> None:
    """第四十一节：禁止关键词硬编码入口。

    用 AST 检查真实的字符串比较节点，不看 docstring
    （文档里需要举例说明"禁止 if '知识库' in text"）。
    """
    import ast

    from dl_senpai.agent.tools import knowledge_tool

    src = __import__("inspect").getsource(knowledge_tool)
    tree = ast.parse(src)

    # 收集所有比较运算里的字符串常量
    string_literals: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for comp in node.comparators:
                if isinstance(comp, ast.Constant) and isinstance(comp.value, str):
                    string_literals.append(comp.value)

    # 也应检查 `x in "知识库"` 形式的左侧常量
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            for op in node.ops:
                if isinstance(op, ast.In):
                    left = node.left
                    if isinstance(left, ast.Constant) and isinstance(left.value, str):
                        string_literals.append(left.value)

    banned = ["知识库", "资料", "文档", "论文", "课件"]
    offenders = [s for s in string_literals if any(b in s for b in banned)]
    assert not offenders, f"知识工具硬编码了关键词入口: {offenders}"


@pytest.mark.asyncio
async def test_knowledge_tool_executes_retrieval() -> None:
    from dl_senpai.agent.tools.knowledge_tool import execute_knowledge
    from dl_senpai.agent.tools.models import ToolCall

    chunks = [KnowledgeChunk(text="死锁四条件", source_name="讲义", score=1.0)]
    retriever = FakeRetriever(chunks=chunks)
    registry = RetrieverRegistry()
    registry.register(retriever, default=True)
    service = KnowledgeService(registry=registry)

    context = _FakeContext(extra={"knowledge_service": service})
    call = ToolCall(tool_name="knowledge", arguments={"query": "死锁"})

    result = await execute_knowledge(context, call)
    assert result.success is True
    assert result.data["count"] == 1
    assert "死锁四条件" in result.output


@pytest.mark.asyncio
async def test_knowledge_tool_empty_query_fails() -> None:
    from dl_senpai.agent.tools.knowledge_tool import execute_knowledge
    from dl_senpai.agent.tools.models import ToolCall

    context = _FakeContext(extra={"knowledge_service": KnowledgeService()})
    result = await execute_knowledge(
        context, ToolCall(tool_name="knowledge", arguments={"query": ""})
    )
    assert result.success is False
    assert result.error_type == "empty_query"


@pytest.mark.asyncio
async def test_knowledge_tool_without_service_fails_cleanly() -> None:
    """没配知识库时行为明确，不崩。"""
    from dl_senpai.agent.tools.knowledge_tool import execute_knowledge
    from dl_senpai.agent.tools.models import ToolCall

    context = _FakeContext(extra={})
    result = await execute_knowledge(
        context, ToolCall(tool_name="knowledge", arguments={"query": "x"})
    )
    assert result.success is False
    assert result.error_type == "no_service"


@pytest.mark.asyncio
async def test_knowledge_tool_permission_denied_is_failure() -> None:
    from dl_senpai.agent.tools.knowledge_tool import execute_knowledge
    from dl_senpai.agent.tools.models import ToolCall

    service, _ = make_service_with_sources()
    context = _FakeContext(
        extra={
            "knowledge_service": service,
            "knowledge_dataset_ids": ["gB"],
            "knowledge_scope": "group",
        },
        user_id="1",
        group_id="100",
    )
    result = await execute_knowledge(
        context, ToolCall(tool_name="knowledge", arguments={"query": "x"})
    )
    assert result.success is False
    assert result.error_type == "permission_denied"


def test_knowledge_tool_visibility() -> None:
    """第五十四节：Tool disabled。"""
    from dl_senpai.agent.tools.knowledge_tool import _visible_knowledge

    assert _visible_knowledge(_FakeContext(extra={})) is False
    assert (
        _visible_knowledge(_FakeContext(extra={"knowledge_service": KnowledgeService()}))
        is True
    )


# ================================================================
# 10. Agent continuation 端到端
# ================================================================


@pytest.mark.asyncio
async def test_knowledge_tool_to_agent_continuation() -> None:
    """第四十三节完整链路：

        User → Agent → knowledge.retrieve → ToolResult → Agent
    """
    from dl_senpai.agent.model_gateway import StubModelGateway
    from dl_senpai.agent.runtime import AgentRuntime
    from dl_senpai.agent.tools.knowledge_tool import build_knowledge_tool
    from dl_senpai.agent.tools.registry import ToolRegistry

    chunks = [
        KnowledgeChunk(
            text="死锁的四个必要条件：互斥、占有并等待、非抢占、循环等待。",
            source_name="操作系统讲义", page=42, score=0.95,
        )
    ]
    retriever = FakeRetriever(chunks=chunks)
    registry_k = RetrieverRegistry()
    registry_k.register(retriever, default=True)
    service = KnowledgeService(registry=registry_k)

    tools = ToolRegistry()
    tools.register(build_knowledge_tool())

    gateway = StubModelGateway(
        replies=[
            "<<<KNOWLEDGE 死锁 必要条件>>>",
            "死锁需要四个条件：互斥、占有并等待、非抢占、循环等待。"
            "（来源：操作系统讲义 第42页）",
        ]
    )
    runtime = AgentRuntime(registry=tools, gateway=gateway)

    context = _FakeContext(extra={"knowledge_service": service})
    result = await runtime.run(context)

    assert result.text.startswith("死锁需要四个条件")
    assert len(result.tool_results) == 1
    assert result.tool_results[0].success is True
    # 检索结果必须回到模型手里（RETURN_TO_MODEL）
    assert "四个必要条件" in result.tool_results[0].output


# ================================================================
# 11. 扩展性：加 provider 不改 Agent
# ================================================================


@pytest.mark.asyncio
async def test_dummy_retriever_is_pluggable() -> None:
    """第五十五节：注册新 Retriever 不需要改 AgentRuntime。"""
    registry = RetrieverRegistry()
    registry.register(DummyRetriever(), default=True)
    service = KnowledgeService(registry=registry)

    result = await service.retrieve(KnowledgeQuery(query="任何东西"))
    assert result.ok
    assert len(result.chunks) == 1
    assert result.provider == "dummy"


def test_registry_register_unregister() -> None:
    registry = RetrieverRegistry()
    registry.register(DummyRetriever(name="d1"))
    assert "d1" in registry
    assert registry.names() == ["d1"]
    registry.unregister("d1")
    assert "d1" not in registry


def test_registry_default_selection() -> None:
    registry = RetrieverRegistry()
    registry.register(DummyRetriever(name="a"))
    registry.register(DummyRetriever(name="b"), default=True)
    assert registry.default_name == "b"
    assert registry.get().name == "b"
    assert registry.get("a").name == "a"


def test_retriever_protocol_is_structural() -> None:
    """Protocol 是结构化类型：不继承也能注册。"""
    from dl_senpai.knowledge import Retriever as RetrieverProto

    assert isinstance(DummyRetriever(), RetrieverProto)
    assert isinstance(FakeRetriever(), RetrieverProto)
    assert isinstance(NullRetriever(), RetrieverProto)
    assert isinstance(InMemoryRetriever(), RetrieverProto)


# ================================================================
# 12. InMemoryRetriever
# ================================================================


@pytest.mark.asyncio
async def test_inmemory_retriever_keyword_match() -> None:
    retriever = InMemoryRetriever()
    retriever.add("死锁需要四个必要条件", source_name="讲义")
    retriever.add("进程调度算法", source_name="讲义")

    result = await retriever.retrieve(KnowledgeQuery(query="死锁"))
    assert result.ok
    assert len(result.chunks) == 1
    assert "死锁" in result.chunks[0].text


@pytest.mark.asyncio
async def test_inmemory_retriever_no_match() -> None:
    retriever = InMemoryRetriever()
    retriever.add("死锁需要四个必要条件")
    result = await retriever.retrieve(KnowledgeQuery(query="量子力学"))
    assert result.is_empty


@pytest.mark.asyncio
async def test_inmemory_retriever_respects_source_access() -> None:
    retriever = InMemoryRetriever()
    retriever.add_source(
        KnowledgeSource(source_id="gA", access=DatasetAccess.GROUP, owner_id="100")
    )
    retriever.add("群A的机密内容", source_id="gA")

    # 群 B 查不到
    result = await retriever.retrieve(
        KnowledgeQuery(query="机密", group_id="200")
    )
    assert result.is_empty

    # 群 A 查得到
    result2 = await retriever.retrieve(
        KnowledgeQuery(query="机密", group_id="100")
    )
    assert len(result2.chunks) == 1


def test_inmemory_retriever_is_not_vector_search() -> None:
    """第五十二节：本阶段禁止 embedding / FAISS / Milvus / pgvector。

    用 AST 检查**真实 import 与调用**，不看 docstring
    （文档里需要举例说明"禁止 faiss"）。
    """
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "plugins" / "dl_senpai" / "knowledge"
    banned = {"faiss", "milvus", "pgvector", "chromadb", "sentence_transformers"}
    offenders: list[str] = []

    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            for mod in mods:
                top = mod.split(".")[0].lower()
                if top in banned:
                    offenders.append(f"{path.name}: import {mod}")
    assert not offenders, f"引入了被禁止的向量组件: {offenders}"


# ================================================================
# 13. RAGFlow 边界
# ================================================================


def test_ragflow_not_configured_by_default() -> None:
    r = RagflowRetriever()
    assert r.configured is False


@pytest.mark.asyncio
async def test_ragflow_returns_error_instead_of_raising(monkeypatch: Any) -> None:
    """契约：检索失败必须转成 KnowledgeResult(ok=False)，**不得抛异常**。

    这条断言原先是 `pytest.raises(NotConfiguredError)`，对应第五十三节
    「只定义边界、没有真实环境」的阶段。接入真实实现后，Retriever Protocol
    要求 retrieve 永不抛异常 —— 知识库查不到不该让整条回复失败。

    这里用替身制造一次真实传输故障，确认异常被就地吞掉并转成错误结果，
    而不是冒泡到 Agent 层。
    """

    class _BoomClient:
        """任何请求都直接抛异常的 httpx.AsyncClient 替身。"""

        def __init__(self, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> "_BoomClient":
            return self

        async def __aexit__(self, *exc: Any) -> bool:
            return False

        async def post(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("boom")

        async def get(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("boom")

    from dl_senpai.knowledge import retrievers

    monkeypatch.setattr(retrievers.httpx, "AsyncClient", _BoomClient)

    r = RagflowRetriever(base_url="http://x", api_key="k", default_dataset_ids=["d1"])
    assert r.configured is True
    res = await r.retrieve(KnowledgeQuery(query="x"))
    assert res.ok is False
    assert res.error == "RuntimeError"
    assert res.chunks == []


def test_ragflow_uses_httpx_with_proxy_bypass() -> None:
    """确认走 httpx 打真实接口，并且**显式绕开系统代理**。

    原断言是「RagflowRetriever 不得出现 httpx」，依据第五十三节「没有真实
    API 环境不要假装实现完成」。现已接入内网 RAGFlow，断言方向改为正向，
    并新增一条真正重要的约束：必须 `trust_env=False` —— 本机开着系统代理时，
    httpx 会把发往内网 10.x 的请求也塞进代理，表现为连接失败或 502。
    """
    import inspect

    from dl_senpai.knowledge import retrievers

    src = inspect.getsource(retrievers.RagflowRetriever)
    assert "httpx" in src
    assert "trust_env=False" in src, "内网请求必须绕开系统代理（踩过的坑）"
    # 仍禁止引入第二套 HTTP 栈：重试/超时语义必须只有一套
    for banned in ("aiohttp", "requests.post", "requests.get"):
        assert banned not in src, f"RagflowRetriever 引入了多余 HTTP 栈: {banned}"


def test_no_real_secrets_in_knowledge_layer() -> None:
    """第五十七节：测试不得使用真实 API Key。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "plugins" / "dl_senpai" / "knowledge"
    for path in root.rglob("*.py"):
        content = path.read_text(encoding="utf-8")
        assert "sk-" not in content, f"{path.name} 含疑似真实密钥"


# ================================================================
# 14. Health / list_sources
# ================================================================


@pytest.mark.asyncio
async def test_health_reports_providers() -> None:
    registry = RetrieverRegistry()
    registry.register(DummyRetriever(), default=True)
    service = KnowledgeService(registry=registry)

    health = await service.health()
    assert health["ok"] is True
    assert "dummy" in health["providers"]
    assert health["details"][0]["name"] == "dummy"


@pytest.mark.asyncio
async def test_health_detects_broken_provider() -> None:
    class Broken:
        name = "broken"

        async def retrieve(self, query: KnowledgeQuery) -> KnowledgeResult:
            raise RuntimeError("down")

        def list_sources(self) -> list[Any]:
            return []

    registry = RetrieverRegistry()
    registry.register(Broken(), default=True)
    service = KnowledgeService(registry=registry)

    health = await service.health()
    assert health["ok"] is False
    assert health["details"][0]["ok"] is False


def test_list_sources_merges() -> None:
    retriever = FakeRetriever(
        sources=[KnowledgeSource(source_id="s1", name="来源1")]
    )
    registry = RetrieverRegistry()
    registry.register(retriever, default=True)
    service = KnowledgeService(registry=registry)
    service.register_source_policy(KnowledgeSource(source_id="s2", name="策略来源"))

    sources = {s.source_id for s in service.list_sources()}
    assert sources == {"s1", "s2"}


@pytest.mark.asyncio
async def test_index_not_implemented() -> None:
    """写入接口本阶段不实现，但必须明确报错而不是静默成功。"""
    service = KnowledgeService()
    with pytest.raises(NotImplementedError):
        await service.index()
    with pytest.raises(NotImplementedError):
        await service.delete()


# ================================================================
# 工具
# ================================================================


class _FakeContext:
    """最小 AgentContext 替身。"""

    def __init__(
        self,
        *,
        extra: dict[str, Any] | None = None,
        user_id: str = "1",
        group_id: str = "100",
        session_id: str = "group:100",
        interrupt: bool = False,
        private_chat: bool = False,
    ) -> None:
        self.extra = extra or {}
        self.user_id = user_id
        self.group_id = group_id
        self.session_id = session_id
        self.interrupt = interrupt
        self.private_chat = private_chat
        self.actor = "测试用户"
        self.plain_text = "测试"
        # AgentContext 新增字段：gateway 合成的「引用块 + 本句」。
        # 替身留空表示"没有引用"，runtime 会回落到 plain_text。
        self.user_text = ""
        self.config = None
        self.messages = []
        self.history: list[dict[str, Any]] = []
        self.trigger_reason = "test"
        self.trace_id = "t"
        self.message_context = None
