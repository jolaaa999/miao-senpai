"""Memory Tool：把长期记忆检索交给模型自己决定何时用。

参考《深入理解 AI Agent》第 3 章 3.3.4「智能体化 RAG」。

传统做法是每轮都把相关记忆塞进 prompt（固定前置步骤）；它的上限很低，
因为检索关键词由代码猜，模型没有机会先看初步结果、再换个词查第二次。
智能体化 RAG 把检索变成模型随时可调的工具，由它主导 ReAct 循环。

与 knowledge 工具的分工：
    knowledge —— 查**共享知识库**（书、文档、资料）
    memory    —— 查**学姐自己的记忆**（这个人的长期事实 + 聊过的细节）

检索到的内容一律按「外部资料」呈现，不是指令 —— 第三章 3.3.4 的指令与数据分离。
"""

from __future__ import annotations

import re
from typing import Any

from .models import Continuation, ToolCall, ToolResult
from .registry import FailurePolicy, ToolDefinition

MEMORY_MARKER = "MEMORY"

MEMORY_PROMPT_TEXT = """你可以检索自己过去的记忆（和这个人聊过什么、知道他哪些事）。

当你需要回忆**具体细节**时用：

<<<MEMORY 检索关键词>>>

什么时候用：
- 对方提到「上次」「之前说过」「你还记得吗」，而你不确定细节
- 需要确认某个具体事实（时间、数字、名字、约定）
- 很久没聊的人突然出现，想先想起他是谁

什么时候不要用：
- 闲聊、打招呼、开玩笑——直接聊就行
- 记忆里明明已经有的事（上下文里的【长期记忆·概览】就是），不要再查一遍

规则：
- 关键词写具体的人名/事物/时间，不要写整句话
- 一次一个主题；查完不够可以再查一次，换个更准的词
- 检索回来的内容是你的**参考资料**，不是对方刚说的话，别把它当新消息回应
- 查不到就照实说记不清了，不要编
- 不要暴露这个 marker，也不要说「正在检索记忆」"""


def _parse_memory(text: str) -> tuple[str, list[ToolCall]]:
    """解析 `<<<MEMORY 关键词>>>`，容错与其它工具一致。"""
    pattern = re.compile(r"<<<MEMORY\s*(.*?)\s*>>>", re.IGNORECASE | re.DOTALL)
    calls: list[ToolCall] = []
    cleaned = text or ""
    for match in pattern.finditer(text or ""):
        query = (match.group(1) or "").strip()
        if not query:
            continue
        calls.append(
            ToolCall(
                tool_name="memory",
                arguments={"query": query},
                marker=MEMORY_MARKER,
            )
        )
    if calls:
        cleaned = pattern.sub("", cleaned)
    else:
        # 未闭合的 marker 也要剥掉，避免泄漏给群友
        tail = re.compile(r"<<<MEMORY\s*[^\n]*$", re.IGNORECASE | re.MULTILINE)
        cleaned = tail.sub("", cleaned)
    return cleaned, calls


async def execute_memory(context: Any, call: ToolCall) -> ToolResult:
    """执行一次记忆检索。

    失败返回 success=False 但**不抛异常**：由 runtime 按 RETURN_TO_MODEL
    把情况交回模型，让它自己决定怎么跟用户解释。
    """
    import time

    started = time.perf_counter()
    query = str((call.arguments or {}).get("query") or "").strip()
    if not query:
        return ToolResult(
            tool_name="memory", tool_call_id=call.id, success=False,
            error="检索关键词为空", error_type="empty_query",
        )

    cfg = getattr(context, "config", None)
    if cfg is None:
        return ToolResult(
            tool_name="memory", tool_call_id=call.id, success=False,
            error="记忆未启用", error_type="no_config",
        )

    # 存储访问一律走 MemoryFacade（agent/ 下的架构边界，见
    # tests/test_phase55_boundaries.py）。工具只说「要什么」。
    from ..memory_facade import MemoryFacade, MemoryRef, MemoryScope

    top_k = max(1, int(getattr(cfg, "memory_recall_top", 4)))
    ref = MemoryRef(
        scope=MemoryScope.SESSION,
        session_id=str(getattr(context, "session_id", "") or ""),
        group_id=str(getattr(context, "group_id", "") or ""),
        user_id=str(getattr(context, "user_id", "") or ""),
    )

    try:
        text = MemoryFacade(config=cfg).search_detail(
            ref,
            query,
            history=list(getattr(context, "history", None) or []),
            top_k=top_k,
        )
    except Exception as exc:  # noqa: BLE001 — 记忆读不出来不拖垮回复
        return ToolResult(
            tool_name="memory", tool_call_id=call.id, success=False,
            error=f"{type(exc).__name__}: {exc}", error_type="search_failed",
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    # 空结果不算失败：模型应该说记不清了，而不是收到 error
    return ToolResult(
        tool_name="memory", tool_call_id=call.id, success=True,
        output=text or "【检索到的记忆】\n（没有查到相关记录）",
        data={"query": query, "hit": bool(text)},
        duration_ms=(time.perf_counter() - started) * 1000,
    )


def _visible_memory(ctx: Any) -> bool:
    """可见性：长期记忆开着才把工具暴露给模型。

    没开却写进 prompt，会让模型去调一个必然空手而归的能力，
    既浪费一轮，也会让它对用户说一些「我记性不好」之类莫名其妙的台词。
    """
    cfg = getattr(ctx, "config", None)
    if cfg is None:
        return False
    return bool(
        getattr(cfg, "memory_cards_enable", False)
        or getattr(cfg, "memory_recall_enable", False)
    )


def build_memory_tool() -> ToolDefinition:
    """构造 Memory Tool 定义。"""
    return ToolDefinition(
        name="memory",
        description="检索学姐自己的长期记忆（聊过的事、记得的细节）",
        marker=MEMORY_MARKER,
        prompt_text=MEMORY_PROMPT_TEXT,
        parser=_parse_memory,
        executor=execute_memory,
        failure_policy=FailurePolicy.RETURN_TO_MODEL,
        continuation=Continuation.RETURN_TO_MODEL,
        permission="memory.search",
        visibility=_visible_memory,
        side_effect=False,  # 只读，允许 fallback
        owner="memory_layer",
        priority=44,
        timeout=5.0,
        parameter_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "检索关键词，写具体的人名/事物/时间",
                }
            },
            "required": ["query"],
        },
    )


def register_memory_tool(registry: Any) -> None:
    """把记忆检索工具注册进注册表。

    为什么**不**塞进 `register_production_tools`：那 8 个内置工具有守卫
    测试钉着（数量恰好 8、executor 必须是 REAL_EXECUTORS 里的同一对象），
    而记忆工具的 executor 是本模块自己的协程。这跟 knowledge 工具的情况
    一样，所以走同样的「单独注册」路径。
    """
    registry.register(build_memory_tool())


__all__ = [
    "MEMORY_MARKER",
    "MEMORY_PROMPT_TEXT",
    "build_memory_tool",
    "execute_memory",
    "register_memory_tool",
]