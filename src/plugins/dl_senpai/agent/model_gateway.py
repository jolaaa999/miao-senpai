"""ModelGateway：Agent 与具体模型客户端之间的边界。

Phase 4 第三十节要求：**AgentRuntime 不应该直接依赖 `OpenAI()`**。

    AgentRuntime → ModelGateway → (LegacyModelGateway | 未来其它实现)
                                      ↓
                                  SenpaiLLM

本阶段不重写 `llm.py`（它是经过真实调优的资产，含本地 3B 降级路径）。
Gateway 只做三件事：
1. 把 Agent 的 `messages` + `options` 翻译成 `SenpaiLLM.chat()` 的调用；
2. 把 `ChatResult` 归一化成 Agent 侧认识的形状；
3. 暴露 token usage（Phase 2 已开始读取 `resp.usage`）。

这样 Phase 5+ 要换成真正的多模型路由时，不需要动 AgentRuntime。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ModelRequest:
    """一次模型调用请求。"""

    # 完整消息列表（system + 历史 + 本轮用户）。保留给测试与诊断读取，
    # 网关不再靠解析它来还原结构——见下面三个结构化字段。
    messages: list[dict[str, Any]] = field(default_factory=list)
    # 结构化字段：由 AgentRuntime 显式填好，网关直接透传。
    # 为什么不再猜 messages 的形状：旧实现把整包 messages 当 history 丢给
    # llm.chat()，导致 runtime 的 system prompt 被降级成一条历史消息、
    # 末尾的用户消息重复发送且人称叠加（群友：小明：你好）。
    system_prompt: str = ""
    history: list[dict[str, Any]] = field(default_factory=list)
    user_text: str = ""
    # 本轮消息里的图片（data URL 或 http URL）。以前没有这个字段，
    # 视觉开关打开也传不进去，模型永远看不到图。
    image_urls: list[str] = field(default_factory=list)
    max_tokens: int = 1600
    temperature: float = 1.05
    timeout: float = 60.0
    # 便于诊断：是否插嘴模式（影响 max_tokens 与 prompt）
    interrupt: bool = False
    session_id: str = ""
    # 原生 function calling 的工具 schema（OpenAI 形状）。
    # 为空 = 走 marker 协议，下游行为与改造前完全一致。
    tools: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ModelResponse:
    """模型返回（Agent 侧形状）。"""

    text: str = ""
    # 原始 ChatResult，保留给 legacy 适配层使用
    raw: Any = None
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    error: str = ""
    # 本次回复中的工具调用（统一 ToolCall 形状，marker / native 皆可）。
    # AgentRuntime 优先用它；为空时才回落到解析 text 里的 marker。
    tool_calls: list[Any] = field(default_factory=list)


class ModelGateway:
    """模型网关接口。"""

    async def chat(self, request: ModelRequest, context: Any = None) -> ModelResponse:
        raise NotImplementedError


class LegacyModelGateway(ModelGateway):
    """把 `SenpaiLLM` 包装成 ModelGateway。

    这是 Phase 4 的**生产实现**。它不做任何 prompt 组装或工具解析
    （那些已分别由 PromptBuilder / MarkerToolAdapter 负责），
    只负责"发消息、收文本"。
    """

    def __init__(self, llm: Any = None) -> None:
        self._llm = llm

    def _get_llm(self, context: Any) -> Any:
        if self._llm is not None:
            return self._llm
        # 本文件位于 src/plugins/dl_senpai/agent/，包名是 ...dl_senpai.agent。
        # `llm.py` 在 dl_senpai/ 下，因此上溯**一层**（两个点）。
        # 原先写的是三个点 `...llm` → 解析成 src.plugins.llm（不存在），
        # 导致 RUNTIME 模式下每次调用都 ModuleNotFoundError 崩溃。
        from ..llm import get_llm

        config = getattr(context, "config", None)
        return get_llm(config)

    async def chat(self, request: ModelRequest, context: Any = None) -> ModelResponse:
        """调用底层 LLM。

        注意：这里调用的是 `SenpaiLLM.chat()` 的**完整形态**，
        包括它内部的 prompt 组装与 marker 解析——因为 Phase 4 采取的是
        "新架构并行建立、行为保持一致"策略，而不在这一步替换生产路径。
        AgentRuntime 通过本 Gateway 获取响应，再决定是否走新解析链。
        """
        llm = self._get_llm(context)
        try:
            result = await llm.chat(
                # 用结构化字段；老调用方没填就回落到从 messages 里拆。
                request.history if request.history else _split_history(request.messages),
                request.user_text or _extract_user_text(request.messages),
                interrupt=request.interrupt,
                sender_name=getattr(context, "actor", "") or "群友",
                image_urls=request.image_urls or None,
                system_prompt_override=request.system_prompt,
                tools=request.tools or None,
            )
        except Exception as exc:  # noqa: BLE001
            return ModelResponse(error=f"{type(exc).__name__}: {exc}")

        return ModelResponse(
            text=getattr(result, "text", "") or "",
            # 必须回传：SenpaiLLM._finalize_result 会把 marker 从 text 里剥掉，
            # 只给 text 的话 AgentRuntime 永远解析不到工具（历史 bug）。
            tool_calls=list(getattr(result, "tool_calls", None) or []),
            raw=result,
            model=getattr(getattr(context, "config", None), "active_model", lambda: "")(),
        )


def _split_history(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """从整包 messages 里剥出真正的历史（去掉 system 与末尾的当前输入）。

    只在调用方没填结构化字段时兜底。目的和 _extract_user_text 一致：
    不能让 system prompt 混进 history，也不能让本轮用户消息既当历史、
    又当 user_text 被发第二遍。
    """
    rest = list(messages or [])
    if rest and rest[0].get("role") == "system":
        rest.pop(0)
    if rest and rest[-1].get("role") == "user":
        rest.pop(-1)
    return rest


def _extract_user_text(messages: list[dict[str, Any]]) -> str:
    """从 messages 末尾取当前用户输入。"""
    for message in reversed(messages or []):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
    return ""


class StubModelGateway(ModelGateway):
    """测试用网关：按序返回预设文本，不触碰网络。"""

    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or [])
        self.requests: list[ModelRequest] = []

    async def chat(self, request: ModelRequest, context: Any = None) -> ModelResponse:
        self.requests.append(request)
        text = self.replies.pop(0) if self.replies else ""
        return ModelResponse(text=text, model="stub")


__all__ = [
    "ModelGateway",
    "LegacyModelGateway",
    "StubModelGateway",
    "ModelRequest",
    "ModelResponse",
]
