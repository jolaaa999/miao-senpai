"""Knowledge Layer：知识检索抽象层。

Phase 6B 第三十一/三十二节。

**Knowledge ≠ Memory**：

    Memory    —— 关于人 / 群 / 当前交互   （agent/memory_facade.py）
    Knowledge —— 关于文档 / 资料 / 外部知识库（本包）

本包**不含任何具体 provider 绑定**：默认只有 NullRetriever，
将来接 RAGFlow 时新增一个 Retriever 并在 registry 注册即可，
Agent / PromptBuilder / ToolExecutor 无需改动。
"""

from __future__ import annotations

from .models import (
    DatasetAccess,
    KnowledgeChunk,
    KnowledgeQuery,
    KnowledgeResult,
    KnowledgeScope,
    KnowledgeSource,
)
from .retriever import (
    NullRetriever,
    Retriever,
    RetrieverRegistry,
    get_retriever_registry,
    reset_retriever_registry,
)
from .retrievers import (
    DummyRetriever,
    FakeRetriever,
    InMemoryRetriever,
    NotConfiguredError,
    RagflowRetriever,
)
from .service import (
    KnowledgeError,
    KnowledgePermissionError,
    KnowledgeService,
)

__all__ = [
    # models
    "KnowledgeScope",
    "DatasetAccess",
    "KnowledgeSource",
    "KnowledgeQuery",
    "KnowledgeChunk",
    "KnowledgeResult",
    # retriever
    "Retriever",
    "RetrieverRegistry",
    "NullRetriever",
    "get_retriever_registry",
    "reset_retriever_registry",
    # implementations
    "FakeRetriever",
    "InMemoryRetriever",
    "DummyRetriever",
    "RagflowRetriever",
    "NotConfiguredError",
    # service
    "KnowledgeService",
    "KnowledgeError",
    "KnowledgePermissionError",
]
