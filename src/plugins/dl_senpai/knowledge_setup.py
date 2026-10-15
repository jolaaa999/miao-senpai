"""知识库装配：把配置变成可用的 KnowledgeService。

`knowledge/` 包只提供抽象层（Retriever / KnowledgeService），**不负责选择
具体 provider**。本模块就是那个「选择与构造」的地方。

为什么放在 `agent/` 之外：
    `tests/test_phase6b_boundaries.py::test_adding_provider_requires_no_agent_change`
    禁止 agent/ 下任何模块 import 具体 provider。Agent 层只应认识
    KnowledgeService 抽象，provider 的构造属于外部装配职责（第五十五节）。

与热重载的关系：
    `get_knowledge_service()` 按「会影响构造结果」的配置做指纹缓存，配置一变
    就重建。这样后台改开关后不必重启 bot，与 `config_watcher` 的设计一致。
"""

from __future__ import annotations

import time
from typing import Any

from .knowledge.models import KnowledgeScope
from .knowledge.retriever import CompositeRetriever, RetrieverRegistry
from .knowledge.retrievers import RagflowRetriever
from .knowledge.service import KnowledgeService
from .observability.logging import log_event

# 进程内缓存：指纹相同就复用同一个 service
_service_cache: KnowledgeService | None = None
_service_key: tuple[Any, ...] = ()

# 预取熔断状态。为什么需要：RAGFlow 故障时每次检索都要等满 knowledge_timeout
# （配置里是 8 秒），而预取在提问之前跑，等于每条事实类消息都白等 8 秒。
# 连续失败到阈值就先停一段时间，成功一次立刻恢复。
_prefetch_fail_streak = 0
_prefetch_cooldown_until = 0.0
_PREFETCH_FAIL_THRESHOLD = 3
_PREFETCH_COOLDOWN_SECONDS = 600.0


def _note_prefetch_result(ok: bool) -> None:
    """记录预取成败并维护熔断状态。"""
    global _prefetch_fail_streak, _prefetch_cooldown_until
    if ok:
        _prefetch_fail_streak = 0
        _prefetch_cooldown_until = 0.0
        return
    _prefetch_fail_streak += 1
    if _prefetch_fail_streak >= _PREFETCH_FAIL_THRESHOLD:
        _prefetch_cooldown_until = time.monotonic() + _PREFETCH_COOLDOWN_SECONDS
        log_event(
            "knowledge.prefetch_circuit_open",
            level="warning",
            streak=_prefetch_fail_streak,
            cooldown_seconds=_PREFETCH_COOLDOWN_SECONDS,
        )


def _fingerprint(config: Any) -> tuple[Any, ...]:
    """会影响 KnowledgeService 构造结果的配置指纹。

    只放「变了就必须重建」的项；top_k / max_chars 这类每次调用都读的
    参数故意不放进来，否则调一下检索条数就要重建整个 provider。
    """
    return (
        bool(getattr(config, "knowledge_enable", False)),
        bool(getattr(config, "knowledge_local_enable", False)),
        str(getattr(config, "knowledge_local_dir", "") or ""),
        str(getattr(config, "knowledge_provider", "") or ""),
        config.knowledge_base_url_resolved(),
        config.knowledge_api_key.strip(),
        tuple(config.knowledge_dataset_id_list()),
        float(config.knowledge_timeout),
        float(config.knowledge_similarity_threshold),
        float(config.knowledge_vector_weight),
    )


def build_knowledge_service(config: Any) -> KnowledgeService | None:
    """按配置构造 KnowledgeService；没配置好则返回 None。

    返回 None（而不是拿 NullRetriever 撑着的 service）是有意的：
    knowledge 工具的可见性判定就是「extra 里有没有 knowledge_service」，
    返回 None 正好让工具保持不可见，模型不会浪费一轮去调一个必然查不到的
    接口 —— 与「默认行为明确」的设计一致。

    来源判定用 knowledge_any_configured()：本地知识库不需要地址密钥，
    服务端挂了它也该照常工作，所以不能拿 RAGFlow 的配置状态当总闸。
    """
    any_configured = getattr(config, "knowledge_any_configured", None)
    if callable(any_configured):
        if not any_configured():
            return None
    else:
        # 兼容没有新方法的旧配置对象：退回只认 RAGFlow
        configured = getattr(config, "knowledge_configured", None)
        if not callable(configured) or not configured():
            return None

    retrievers: list[Any] = []
    names: list[str] = []

    # 1) 本地 Markdown 知识库（零依赖，永远先加）
    if bool(getattr(config, "knowledge_local_enable", False)):
        try:
            from .knowledge.local_kb import LocalMarkdownRetriever

            local_root = config.knowledge_local_dir_resolved()
            retrievers.append(
                LocalMarkdownRetriever(local_root, name="local_md")
            )
            names.append("local_md")
        except Exception as exc:  # noqa: BLE001 — 本地库起不来不该阻断 RAGFlow
            log_event(
                "knowledge.local_retriever_failed",
                level="warning",
                error=type(exc).__name__,
            )

    # 2) RAGFlow：只有真配好了才加进来
    configured = getattr(config, "knowledge_configured", None)
    provider = str(getattr(config, "knowledge_provider", "") or "ragflow").strip().lower()
    if callable(configured) and configured():
        if provider != "ragflow":
            log_event("knowledge.unknown_provider", level="warning", provider=provider)
        else:
            retrievers.append(RagflowRetriever(
                base_url=config.knowledge_base_url_resolved(),
                api_key=config.knowledge_api_key.strip(),
                name="ragflow",
                timeout=float(config.knowledge_timeout),
                similarity_threshold=float(config.knowledge_similarity_threshold),
                vector_weight=float(config.knowledge_vector_weight),
                default_dataset_ids=config.knowledge_dataset_id_list(),
            ))
            names.append("ragflow")

    if not retrievers:
        return None

    # 两个来源同时存在就用组合检索器：轮转交替，保证谁都不会被挤掉
    retriever = (
        retrievers[0] if len(retrievers) == 1
        else CompositeRetriever(retrievers, name="hybrid")
    )

    registry = RetrieverRegistry()
    registry.register(retriever, default=True)

    log_event(
        "knowledge.service_ready",
        provider=provider,
        sources=names,
        base_url=config.knowledge_base_url_resolved(),
        dataset_count=len(config.knowledge_dataset_id_list()),
        top_k=int(config.knowledge_top_k),
    )

    return KnowledgeService(
        registry=registry,
        timeout=float(config.knowledge_timeout),
        max_chunks=int(config.knowledge_max_chunks),
        max_chars=int(config.knowledge_max_chars),
    )


def get_knowledge_service(config: Any) -> KnowledgeService | None:
    """取（并缓存）KnowledgeService；配置变化自动重建。"""
    global _service_cache, _service_key

    key = _fingerprint(config)
    if key != _service_key:
        _service_cache = build_knowledge_service(config)
        _service_key = key
    return _service_cache


def reset_knowledge_service() -> None:
    """清空缓存与熔断状态（测试用）。"""
    global _service_cache, _service_key
    global _prefetch_fail_streak, _prefetch_cooldown_until
    _service_cache = None
    _service_key = ()
    _prefetch_fail_streak = 0
    _prefetch_cooldown_until = 0.0


def knowledge_extra(config: Any, service: KnowledgeService | None) -> dict[str, Any]:
    """构造注入 `AgentContext.extra` 的知识库键。

    键名必须与 `agent/tools/knowledge_tool.py` 读取的一致（该模块 117-122 行）：
        knowledge_service / knowledge_dataset_ids / knowledge_scope / knowledge_top_k
    """
    if service is None:
        return {}
    # knowledge_enable 的语义是「把检索工具暴露给模型」。
    # 本地库默认开启，若只看 service 存不存在，这个开关就会被静默架空 ——
    # 所以这里显式再判一次。（预取走 knowledge_auto，不受本开关影响。）
    if not bool(getattr(config, "knowledge_enable", False)):
        return {}
    return {
        "knowledge_service": service,
        "knowledge_dataset_ids": config.knowledge_dataset_id_list(),
        "knowledge_scope": KnowledgeScope.GLOBAL.value,
        "knowledge_top_k": int(config.knowledge_top_k),
    }


async def knowledge_prefetch(
    text: str,
    config: Any,
    *,
    top_k: int | None = None,
) -> tuple[str, str]:
    """调用模型前先查一次知识库，返回 (给模型看的文本, 失败原因)。

    为什么要有这条路径：
        knowledge 工具只在模型主动写 <<<KNOWLEDGE>>> 时才跑，而模型经常想不起
        来查。混合检索要求知识库与联网结果出现在同一次提问里，所以这里像联网
        预取那样，先把资料摆到它面前。

    与工具路径的分工：
        本函数不改工具的可见性、权限与 dataset 选择，只是复用同一个 service。
        任何异常都吞掉并返回原因 —— 预取失败绝不该让整条消息发不出去。
        同时**不抛异常、不返回 error 字段**：这里的语义是「有没有额外资料」，
        而不是「某次工具调用成不成功」。
    """
    global _prefetch_fail_streak, _prefetch_cooldown_until

    query_text = (text or "").strip()
    if not query_text:
        return "", "empty_query"

    if time.monotonic() < _prefetch_cooldown_until:
        return "", "cooldown"

    any_configured = getattr(config, "knowledge_any_configured", None)
    if callable(any_configured):
        if not any_configured():
            return "", "not_configured"
    else:
        configured = getattr(config, "knowledge_configured", None)
        if not callable(configured) or not configured():
            return "", "not_configured"

    service = get_knowledge_service(config)
    if service is None:
        return "", "no_service"

    from .knowledge.models import KnowledgeQuery, KnowledgeScope

    header = str(getattr(config, "knowledge_prompt_header", "【知识库检索结果】"))

    try:
        result = await service.retrieve(
            KnowledgeQuery(
                query=query_text,
                scope=KnowledgeScope.GLOBAL,
                dataset_ids=config.knowledge_dataset_id_list(),
                top_k=int(top_k if top_k is not None else config.knowledge_top_k),
            )
        )
    except Exception as exc:  # noqa: BLE001 — 预取失败不能拖垮回复
        _note_prefetch_result(False)
        log_event(
            "knowledge.prefetch_failed",
            level="warning",
            error=type(exc).__name__,
        )
        return "", type(exc).__name__

    if not result.ok:
        _note_prefetch_result(False)
        return "", str(result.error or "failed")

    # 能拿到应答就算成功，哪怕没有命中 —— 空结果说明查得到、只是没资料
    _note_prefetch_result(True)
    if not result.chunks:
        # 空结果也要留一句话：让模型知道「查过了但没查到」，
        # 不然它会以为系统压根没查，回头继续凭记忆答。
        return f"{header}\n（知识库里没有找到相关内容）", ""
    return result.to_prompt_text(), ""


def register_knowledge_tool(registry: Any) -> None:
    """把 knowledge 工具注册进工具表（幂等）。

    为什么不顺手让 `register_production_tools` 注册它：
        那个函数的语义是「8 个生产工具」，且被约 20 个测试直接依赖。
        知识工具默认**不可见**（要上下文里带 knowledge_service 才暴露），
        它并不是那 8 个之一；混进去会让所有基于「8 个工具」的断言失去意义。

    幂等性：ToolRegistry.register 按 name 覆盖，重复调用不会产生重复项。
    """
    try:
        from .agent.tools.knowledge_tool import build_knowledge_tool

        registry.register(build_knowledge_tool())
    except Exception as exc:  # noqa: BLE001 — 注册失败不应阻断 Agent 层构建
        log_event(
            "knowledge.tool_register_failed",
            level="warning",
            error=type(exc).__name__,
        )


__all__ = [
    "build_knowledge_service",
    "knowledge_prefetch",
    "get_knowledge_service",
    "reset_knowledge_service",
    "knowledge_extra",
    "register_knowledge_tool",
]