"""Knowledge Tool：把知识检索暴露给 Agent。

Phase 6B 第四十/四十一/四十二/四十三节。

完整链路：

    用户：「根据我上传的操作系统资料解释死锁」
        ↓
    Agent（语义判断：这需要查资料）
        ↓
    knowledge.retrieve             ← 本工具
        ↓
    KnowledgeService               ← 权限 / 超时 / 预算 / 观测
        ↓
    Retriever（Fake / InMemory / 将来 RAGFlow）
        ↓
    KnowledgeResult → ToolResult
        ↓
    Agent 继续生成答案

第四十一节：**禁止关键词硬编码入口。** 本模块里**没有**
`if "知识库" in text` 这种判断 —— 是否调用由模型自己决定，
这正是整个项目「什么时候不该调用 AI / Tool」原则的延续。

第四十二节：Continuation = RETURN_TO_MODEL，
FailurePolicy = RETURN_TO_MODEL。
    为什么失败也要回模型：检索失败时模型应该能说
    「我暂时查不到那份资料」，而不是整条回复失败。
"""

from __future__ import annotations

from typing import Any

from .marker_adapter import MarkerToolAdapter  # noqa: F401  (类型参考)
from .models import Continuation, ToolCall, ToolResult
from .registry import FailurePolicy, ToolDefinition

# marker 名。与其它工具一致使用 `<<<NAME ...>>>` 协议。
KNOWLEDGE_MARKER = "KNOWLEDGE"

# 提示词。**简短**，因为是否需要检索由模型判断，
# 我们只告诉它"有这个能力 + 怎么用"。
KNOWLEDGE_PROMPT_TEXT = """你可以检索知识库（专业书籍与技术资料）来回答问题。

当用户问的是**知识性、技术性、专业性**问题（概念、原理、机制、区别、
为什么），或者明确提到"资料/知识库/书里"时，用：

<<<KNOWLEDGE 检索关键词>>>

规则：
- 只在需要准确的专业知识时用；闲聊、情感交流、玩笑不要用
- 关键词要具体，一次一个主题（"死锁 四个必要条件" 优于 "那个东西"）
- 只写一行，不要连着堆多个 marker
- 检索结果会作为一条消息回到你手里，你再据此组织回答；用到资料就自然带一句来源
- 那条结果是**你自己查到的**，不是用户发的 —— 别说"你贴的""你发的"这种话
- 检索不到就照实说不知道，不要编造
- 不要向用户暴露这个 marker，也不要说"正在检索数据库"这类机械话"""


def _parse_knowledge(text: str) -> tuple[str, list[ToolCall]]:
    """解析 `<<<KNOWLEDGE 关键词>>>`。

    容错与其它工具保持一致（闭合符 `>>>`）。
    空关键词不产生调用 —— 避免模型写个裸 marker 就触发检索。
    """
    import re

    pattern = re.compile(
        r"<<<KNOWLEDGE\s*(.*?)\s*>>>",
        re.IGNORECASE | re.DOTALL,
    )
    calls: list[ToolCall] = []
    cleaned = text or ""

    for match in pattern.finditer(text or ""):
        query = (match.group(1) or "").strip()
        if not query:
            continue
        calls.append(
            ToolCall(
                tool_name="knowledge",
                arguments={"query": query},
                marker=KNOWLEDGE_MARKER,
            )
        )

    if calls:
        cleaned = pattern.sub("", cleaned)
    else:
        # 未闭合的 `<<<KNOWLEDGE` 也要剥掉，避免泄漏给群友
        tail = re.compile(r"<<<KNOWLEDGE\s*[^\n]*$", re.IGNORECASE | re.MULTILINE)
        cleaned = tail.sub("", cleaned)

    return cleaned, calls


async def execute_knowledge(context: Any, call: ToolCall) -> ToolResult:
    """执行知识检索。

    失败一律返回 `success=False` 但**不抛异常** ——
    由 runtime 按 FailurePolicy.RETURN_TO_MODEL 把失败信息
    交回模型，让它决定怎么跟用户解释。
    """
    import time

    from ...knowledge.models import KnowledgeQuery, KnowledgeScope
    from ...knowledge.service import KnowledgePermissionError, KnowledgeService

    started = time.perf_counter()
    query_text = str((call.arguments or {}).get("query") or "").strip()
    if not query_text:
        return ToolResult(
            tool_name="knowledge", tool_call_id=call.id, success=False,
            error="检索关键词为空", error_type="empty_query",
        )

    # dataset / scope 由上下文决定，不由模型指定 ——
    # 避免模型越权指定别的群的私有库（第四十七节）
    dataset_ids = list((context.extra or {}).get("knowledge_dataset_ids") or [])
    scope = KnowledgeScope((context.extra or {}).get("knowledge_scope") or "global")
    top_k = int((context.extra or {}).get("knowledge_top_k") or 5)

    service: KnowledgeService | None = (context.extra or {}).get(
        "knowledge_service"
    )
    if service is None:
        # 没有配置知识库服务时，行为明确：告诉模型"没有可用的知识库"
        return ToolResult(
            tool_name="knowledge", tool_call_id=call.id, success=False,
            error="知识库未配置", error_type="no_service",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    query = KnowledgeQuery(
        query=query_text,
        scope=scope,
        user_id=str(context.user_id or ""),
        group_id=str(context.group_id or ""),
        dataset_ids=dataset_ids,
        top_k=top_k,
    )

    try:
        result = await service.retrieve(query)
    except KnowledgePermissionError as exc:
        return ToolResult(
            tool_name="knowledge", tool_call_id=call.id, success=False,
            error=str(exc), error_type="permission_denied",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    except Exception as exc:  # noqa: BLE001 — 知识层故障不拖垮回复
        return ToolResult(
            tool_name="knowledge", tool_call_id=call.id, success=False,
            error=f"{type(exc).__name__}: {exc}", error_type="retrieve_failed",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    if not result.ok:
        return ToolResult(
            tool_name="knowledge", tool_call_id=call.id, success=False,
            error=result.error or "检索失败", error_type=result.error or "failed",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    # 空结果不算失败 —— 模型应该说"查不到"，而不是收到 error
    return ToolResult(
        tool_name="knowledge", tool_call_id=call.id, success=True,
        output=result.to_prompt_text(),
        data={
            "value": result,
            "chunks": [
                {
                    "text": c.text,
                    "score": c.score,
                    "source_name": c.source_name,
                    "page": c.page,
                    "section": c.section,
                    "uri": c.uri,
                }
                for c in result.chunks
            ],
            "count": len(result.chunks),
            "provider": result.provider,
        },
        duration_ms=(time.perf_counter() - started) * 1000,
    )


def _visible_knowledge(ctx: Any) -> bool:
    """可见性：只有配置了 KnowledgeService 才把工具暴露给模型。

    没配知识库却把工具写进 prompt，会让模型去调一个必然失败的能力。
    """
    extra = getattr(ctx, "extra", None) or {}
    if extra.get("knowledge_service") is not None:
        return True
    # 也允许通过 feature flag 显式打开（便于测试与渐进上线）
    getter = getattr(ctx, "feature_enabled", None)
    if callable(getter):
        try:
            return bool(getter("knowledge"))
        except Exception:  # noqa: BLE001
            return False
    return False


def build_knowledge_tool() -> ToolDefinition:
    """构造 Knowledge Tool 定义（第四十二节）。

    Continuation = RETURN_TO_MODEL —— 检索结果要回到模型手里，
    它才能据此组织答案。
    FailurePolicy = RETURN_TO_MODEL —— 检索失败也要让模型知道，
    由它决定怎么解释，而不是整条回复失败。
    """
    return ToolDefinition(
        name="knowledge",
        description="检索知识库/资料库（文档、课件、论文、项目文档）",
        marker=KNOWLEDGE_MARKER,
        prompt_text=KNOWLEDGE_PROMPT_TEXT,
        parser=_parse_knowledge,
        executor=execute_knowledge,
        failure_policy=FailurePolicy.RETURN_TO_MODEL,
        continuation=Continuation.RETURN_TO_MODEL,
        permission="knowledge.retrieve",
        visibility=_visible_knowledge,
        side_effect=False,  # 只读，允许 fallback
        owner="knowledge_layer",
        priority=45,
        timeout=10.0,
        parameter_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索关键词，要具体",
                }
            },
            "required": ["query"],
        },
    )


__all__ = [
    "KNOWLEDGE_MARKER",
    "KNOWLEDGE_PROMPT_TEXT",
    "build_knowledge_tool",
    "execute_knowledge",
]
