"""Phase 6B：Knowledge Layer 架构边界测试。

防止架构漂移：
    - Agent 不得直接认识 RAGFlow（第三十四节）
    - Knowledge 与 Memory 不得混淆（第三十二节）
    - Knowledge 不得自动塞 Prompt（第四十四节）
    - Knowledge 检索结果不得写入 Memory（第四十六节）
    - 新增 provider 不得需要改 Agent（第五十五节）
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "src" / "plugins" / "dl_senpai"
AGENT = PKG / "agent"
KNOWLEDGE = PKG / "knowledge"


def code_strings(path: Path) -> list[str]:
    """收集 AST 中真实使用的字符串常量。

    **排除 docstring** —— 文档里需要画架构图（例如
    "Agent → KnowledgeService → Retriever"）或说明禁止项
    （例如"不要直接命名 RagflowRetriever"），那些是文档不是依赖。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))

    # 先收集所有 docstring 节点的对象 id，稍后跳过
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(
            node,
            (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ):
            body = getattr(node, "body", None)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstrings.add(id(body[0].value))

    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            out.append(node.value)
        if isinstance(node, ast.Attribute):
            out.append(node.attr)
        if isinstance(node, ast.Name):
            out.append(node.id)
        if isinstance(node, ast.ImportFrom):
            out.append(node.module or "")
            out.extend(a.name for a in node.names)
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
    return out


# ================================================================
# 1. Agent 不得直接认识 RAGFlow
# ================================================================


def test_agent_layer_does_not_know_ragflow() -> None:
    """第三十四节：禁止 agent_runtime.query_ragflow() 这类直接绑定。"""
    offenders: list[str] = []
    for path in AGENT.rglob("*.py"):
        for token in code_strings(path):
            if "ragflow" in token.lower():
                offenders.append(f"{path.relative_to(AGENT)}: {token}")
    assert not offenders, (
        "Agent 层直接引用了 RAGFlow（应只通过 KnowledgeService）:\n"
        + "\n".join(offenders)
    )


def test_prompt_builder_does_not_know_knowledge() -> None:
    """Knowledge 不自动塞 Prompt（第四十四节）。"""
    path = AGENT / "prompt" / "builder.py"
    for token in code_strings(path):
        assert "knowledge" not in token.lower(), (
            f"PromptBuilder 引用了 knowledge: {token}"
        )
    src = path.read_text(encoding="utf-8")
    assert "KnowledgeResult" not in src


def test_tool_executor_does_not_know_knowledge() -> None:
    """ToolExecutor 不认识知识库 —— 它只认 ToolDefinition。"""
    path = AGENT / "tools" / "executor.py"
    for token in code_strings(path):
        assert "knowledge" not in token.lower(), (
            f"ToolExecutor 引用了 knowledge: {token}"
        )


# ================================================================
# 2. Knowledge ≠ Memory
# ================================================================


def test_knowledge_layer_does_not_import_memory() -> None:
    """第三十二节：Knowledge 是文档层，不是记忆层。"""
    memory_modules = {
        "memory", "memory_recall", "person_memory",
        "affection", "inner_state", "group_style",
    }
    offenders: list[str] = []
    for path in KNOWLEDGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                base = (node.module or "").rsplit(".", 1)[-1]
                if base in memory_modules:
                    offenders.append(f"{path.name} imports {node.module}")
    assert not offenders, (
        "Knowledge 层引用了 Memory 模块:\n" + "\n".join(offenders)
    )


def test_knowledge_service_does_not_write_memory() -> None:
    """第四十六节：检索到的内容不得自动写入 user memory。"""
    src = (KNOWLEDGE / "service.py").read_text(encoding="utf-8")
    for banned in ("record_person_updates", "MemoryFacade", "append_turn"):
        assert banned not in src, f"KnowledgeService 写入了 Memory: {banned}"


def test_memory_facade_does_not_know_knowledge() -> None:
    """反向也成立：Memory 不该知道 Knowledge。"""
    path = AGENT / "memory_facade.py"
    for token in code_strings(path):
        assert "knowledge" not in token.lower(), (
            f"MemoryFacade 引用了 knowledge: {token}"
        )


# ================================================================
# 3. 分层：Agent → Tool → Service → Retriever
# ================================================================


def test_service_does_not_import_concrete_retrievers() -> None:
    """Service 只依赖 Protocol，不依赖具体的 provider 实现。

    用 AST 检查 import 语句，不看 docstring
    （架构图里会写到 provider 名字）。
    """
    path = KNOWLEDGE / "service.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    banned = {
        "FakeRetriever", "InMemoryRetriever", "DummyRetriever",
        "RagflowRetriever",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in banned:
                    offenders.append(f"import {alias.name}")
    assert not offenders, (
        f"KnowledgeService 直接依赖了具体 provider: {offenders}"
    )


def test_retriever_module_does_not_import_service() -> None:
    """Retriever 是最底层，不该反向依赖 Service（避免循环）。"""
    path = KNOWLEDGE / "retriever.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or "").rsplit(".", 1)[-1]
            assert mod != "service", "retriever.py 反向依赖了 service"
            for alias in node.names:
                assert alias.name != "KnowledgeService", (
                    "retriever.py 反向依赖了 KnowledgeService"
                )


def test_models_has_no_dependencies() -> None:
    """models.py 必须零内部依赖 —— 否则会成为循环依赖的源头。"""
    path = KNOWLEDGE / "models.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"models.py 出现了相对 import: {node.module}"
            assert "dl_senpai" not in (node.module or ""), (
                f"models.py 依赖了包内模块: {node.module}"
            )


def test_knowledge_tool_delegates_to_service() -> None:
    """Knowledge Tool 只调 Service，不直接调 Retriever。"""
    path = AGENT / "tools" / "knowledge_tool.py"
    src = path.read_text(encoding="utf-8")
    assert "KnowledgeService" in src
    for banned in ("FakeRetriever", "InMemoryRetriever", "RagflowRetriever"):
        assert banned not in src, f"Knowledge Tool 直连了 provider: {banned}"


# ================================================================
# 4. 扩展性：新增 provider 不改 Agent
# ================================================================


def test_adding_provider_requires_no_agent_change() -> None:
    """第五十五节：注册新 Retriever 只动 registry。

    判定：agent/ 下没有任何模块 import knowledge 的具体 provider。
    """
    offenders: list[str] = []
    for path in AGENT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name in {
                        "FakeRetriever", "InMemoryRetriever", "DummyRetriever",
                        "RagflowRetriever", "NullRetriever",
                    }:
                        offenders.append(f"{path.relative_to(AGENT)}: {alias.name}")
    assert not offenders, (
        "Agent 层直接依赖了具体 provider:\n" + "\n".join(offenders)
    )


# ================================================================
# 5. 禁止项
# ================================================================


def test_no_embedding_or_vector_db_dependencies() -> None:
    """第五十二节：不自己造向量库。"""
    banned = {"faiss", "milvus", "pgvector", "chromadb", "sentence_transformers",
              "pinecone", "weaviate", "qdrant"}
    offenders: list[str] = []
    for path in KNOWLEDGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            for mod in mods:
                if mod.split(".")[0].lower() in banned:
                    offenders.append(f"{path.name}: {mod}")
    assert not offenders, f"引入了向量库依赖: {offenders}"


def test_no_real_secrets_anywhere_in_knowledge() -> None:
    """第五十七节：不得出现真实 API Key。"""
    for path in KNOWLEDGE.rglob("*.py"):
        content = path.read_text(encoding="utf-8")
        assert "sk-" not in content, f"{path.name} 含疑似真实密钥"
        assert "api_key=\"ey" not in content


def test_ragflow_retriever_is_really_implemented() -> None:
    """第五十三节：RAGFlow 边界已按预留方式接入真实 API。

    这条断言的方向是**从反向改成正向**的。原断言禁止 RagflowRetriever 出现
    任何 HTTP 调用，依据是第五十三节「没有真实 API 测试环境时不要假装实现
    完成」。现在有了内网 RAGFlow 与可用 dataset，就按同一节预留的扩展点把
    实现补齐；断言随之改为正向 —— 否则测试会反过来阻止使用真实能力。
    """
    src = (KNOWLEDGE / "retrievers.py").read_text(encoding="utf-8")
    # 未配置路径仍必须明确报错，不能静默返回空结果
    assert "NotConfiguredError" in src
    tree = ast.parse(src)
    found = False
    class_attrs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "RagflowRetriever":
            found = True
            class_attrs = {
                sub.attr for sub in ast.walk(node) if isinstance(sub, ast.Attribute)
            }
    assert found, "未找到 RagflowRetriever 类定义"
    assert {"post", "get"} <= class_attrs, (
        "RagflowRetriever 应调用 /api/v1/retrieval(POST) 与 /api/v1/datasets(GET)"
    )


class _FakeResponse:
    """httpx.Response 的最小替身。"""

    def __init__(self, status_code: int, payload: dict) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _FakeAsyncClient:
    """httpx.AsyncClient 的替身：记录请求、回放预置响应，**完全不联网**。

    之所以能替换：retrievers.py 通过模块属性 `httpx.AsyncClient` 构造 client，
    monkeypatch 该属性即可拦截全部出入口，测试因此不依赖真实 RAGFlow。
    """

    created: list["_FakeAsyncClient"] = []
    responses: list[_FakeResponse] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.calls: list[tuple[str, dict]] = []
        _FakeAsyncClient.created.append(self)

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def post(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append((url, dict(kwargs)))
        return _FakeAsyncClient.responses.pop(0)

    async def get(self, url: str, **kwargs: object) -> _FakeResponse:
        self.calls.append((url, dict(kwargs)))
        return _FakeAsyncClient.responses.pop(0)


def _install_fake_client(monkeypatch: object, responses: list[tuple[int, dict]]) -> type:
    """把 retrievers 模块里的 httpx.AsyncClient 换成替身。"""
    from dl_senpai.knowledge import retrievers as mod

    _FakeAsyncClient.created = []
    _FakeAsyncClient.responses = [_FakeResponse(s, p) for s, p in responses]
    monkeypatch.setattr(mod.httpx, "AsyncClient", _FakeAsyncClient)
    return _FakeAsyncClient


def test_ragflow_retriever_never_raises_when_unconfigured() -> None:
    """契约：失败必须转成 KnowledgeResult(ok=False)，而不是抛异常。"""
    import asyncio

    from dl_senpai.knowledge.models import KnowledgeQuery
    from dl_senpai.knowledge.retrievers import RagflowRetriever

    res = asyncio.run(RagflowRetriever().retrieve(KnowledgeQuery(query="任意")))
    assert res.ok is False
    assert res.error == "not_configured"
    assert res.chunks == []


def test_ragflow_retriever_maps_and_truncates(monkeypatch: object) -> None:
    """字段映射正确，并且**自己截断 top_k**（服务端实测会忽略 top_k）。"""
    import asyncio

    from dl_senpai.knowledge.models import KnowledgeQuery
    from dl_senpai.knowledge.retrievers import RagflowRetriever

    payload = {
        "code": 0,
        "data": {
            "total": 30,
            "chunks": [
                {
                    "id": f"c{i}",
                    "content": f"正文{i}",
                    "similarity": 0.9 - i * 0.1,
                    "document_id": "doc1",
                    "document_keyword": "操作系统概念（原书第9版） ( etc.) "
                    "(z-library.sk, 1lib.sk).pdf",
                }
                for i in range(6)
            ],
        },
    }
    cls = _install_fake_client(monkeypatch, [(200, payload)])

    retriever = RagflowRetriever(
        base_url="http://example.invalid/prefix",
        api_key="k",
        default_dataset_ids=["d1"],
    )
    res = asyncio.run(retriever.retrieve(KnowledgeQuery(query="死锁", top_k=2)))

    assert res.ok is True
    assert len(res.chunks) == 2, "必须截断到 top_k（服务端会返回全部）"
    assert res.chunks[0].score >= res.chunks[1].score, "必须按 score 降序"
    assert res.chunks[0].chunk_id == "c0"
    assert res.chunks[0].source_id == "doc1"
    # 来源名必须已清洗掉下载站噪音，否则会被原样引用给用户
    assert res.chunks[0].source_name == "操作系统概念（原书第9版）"
    assert "z-library" not in res.chunks[0].source_name
    # 原始文件名保留在 metadata，便于回查原文
    assert "z-library" in res.chunks[0].metadata["raw_name"]

    client = cls.created[0]
    url, kwargs = client.calls[0]
    assert url == "http://example.invalid/prefix/api/v1/retrieval"
    assert kwargs["json"]["dataset_ids"] == ["d1"]
    assert kwargs["json"]["top_k"] == 2
    # 内网地址必须绕开系统代理（踩过的坑，不能回退）
    assert client.kwargs.get("trust_env") is False


def test_ragflow_retriever_discovers_dataset_ids(monkeypatch: object) -> None:
    """未指定 dataset 时先用 /api/v1/datasets 发现（本部署强制要 dataset_ids）。"""
    import asyncio

    from dl_senpai.knowledge.models import KnowledgeQuery
    from dl_senpai.knowledge.retrievers import RagflowRetriever

    ds = {"code": 0, "data": [{"id": "d1"}, {"id": "d2"}]}
    ok = {
        "code": 0,
        "data": {
            "total": 1,
            "chunks": [
                {
                    "id": "c1",
                    "content": "命中",
                    "similarity": 0.5,
                    "document_id": "doc1",
                    "document_keyword": "book.pdf",
                }
            ],
        },
    }
    cls = _install_fake_client(monkeypatch, [(200, ds), (200, ok)])

    retriever = RagflowRetriever(base_url="http://example.invalid/p", api_key="k")
    res = asyncio.run(retriever.retrieve(KnowledgeQuery(query="x", top_k=3)))

    assert res.ok is True
    assert len(res.chunks) == 1
    client = cls.created[0]
    assert client.calls[0][0].endswith("/api/v1/datasets")
    assert client.calls[1][1]["json"]["dataset_ids"] == ["d1", "d2"]


def test_ragflow_retriever_reports_http_error_without_raising(monkeypatch: object) -> None:
    """HTTP 非 200 → ok=False，不抛异常。"""
    import asyncio

    from dl_senpai.knowledge.models import KnowledgeQuery
    from dl_senpai.knowledge.retrievers import RagflowRetriever

    _install_fake_client(monkeypatch, [(500, {})])
    retriever = RagflowRetriever(
        base_url="http://example.invalid/p", api_key="k", default_dataset_ids=["d1"]
    )
    res = asyncio.run(retriever.retrieve(KnowledgeQuery(query="x")))
    assert res.ok is False
    assert res.error == "http_500"


def test_ragflow_retriever_reports_business_error_without_raising(monkeypatch: object) -> None:
    """RAGFlow 用 code!=0 表达业务错误（例如 dataset_ids 缺失）。"""
    import asyncio

    from dl_senpai.knowledge.models import KnowledgeQuery
    from dl_senpai.knowledge.retrievers import RagflowRetriever

    _install_fake_client(
        monkeypatch,
        [(200, {"code": 102, "message": "`dataset_ids` is required."})],
    )
    retriever = RagflowRetriever(
        base_url="http://example.invalid/p", api_key="k", default_dataset_ids=["d1"]
    )
    res = asyncio.run(retriever.retrieve(KnowledgeQuery(query="x")))
    assert res.ok is False
    assert "required" in res.error


def test_knowledge_is_a_real_package() -> None:
    """不是空壳包。"""
    for path in KNOWLEDGE.rglob("*.py"):
        src = path.read_text(encoding="utf-8").strip()
        assert len(src) > 300, f"{path.name} 疑似空壳（{len(src)} 字符）"


def test_knowledge_namespace_registered() -> None:
    """Knowledge Tool 能注册进 ToolRegistry（第四十节）。"""
    from dl_senpai.agent.tools.knowledge_tool import build_knowledge_tool
    from dl_senpai.agent.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(build_knowledge_tool())
    assert "knowledge" in registry.names()
    assert registry.get("knowledge").marker == "KNOWLEDGE"


def test_knowledge_marker_does_not_collide() -> None:
    """KNOWLEDGE 不得与既有 8 个 marker 冲突。"""
    from dl_senpai.agent.tools.builtin import build_production_tools
    from dl_senpai.agent.tools.knowledge_tool import KNOWLEDGE_MARKER

    existing = {t.marker for t in build_production_tools()}
    assert KNOWLEDGE_MARKER not in existing, "KNOWLEDGE marker 与既有工具冲突"
