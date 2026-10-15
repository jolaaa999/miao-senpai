"""Agent 上下文、状态与结果。

这三个模型的职责边界（第六/七/八节）：

    AgentContext —— 一轮执行**需要知道的环境**（不可变输入）
    AgentState   —— 一轮执行**过程中的累积**（单轮运行时状态，不持久化）
    AgentResult  —— 一轮执行**结束后交给上层的答案**

关键设计约束：**AgentContext 只提供 Agent 真正需要的上下文**，
不是把整个系统塞进去（第六节明确要求）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .guard import StopReason, TokenUsage
from .tools.models import ToolCall, ToolResult


@dataclass
class AgentContext:
    """一次 Agent 执行的输入上下文。

    Attributes:
        session_id: 会话（记忆）作用域。
        message_context: gateway 归一化的消息上下文（MessageContext）。
        trigger_reason: 触发原因（private / mention / interrupt）。
        config: 配置对象。
        history: 已加载的历史消息（OpenAI messages 格式）。
        memory: 记忆 facade（当前是薄封装，见 memory_provider）。
        environment: 环境信息（当前时间、群名、昵称等已渲染文本）。
        services: ServiceRegistry（取 llm / reply 等能力）。
        trace_id: 追踪 id。
        interrupt: 是否插嘴模式（影响 prompt 与 max_tokens）。
        private_chat: 是否私聊。
        extra: 扩展位。
    """

    session_id: str = ""
    message_context: Any = None
    trigger_reason: str = ""
    config: Any = None
    history: list[dict[str, Any]] = field(default_factory=list)
    memory: Any = None
    environment: dict[str, Any] = field(default_factory=dict)
    services: Any = None
    trace_id: str = ""
    interrupt: bool = False
    private_chat: bool = False
    extra: dict[str, Any] = field(default_factory=dict)
    # gateway 层已渲染好的「引用块 + 本句」，供 runtime 当作用户输入。
    # 为什么不直接改写 message_context.plain_text：plain_text 是 gateway 语义的
    # 原始消息，被权限判断 / 命令匹配 / should_ignore_text 多处读取，
    # 把引用块塞进去会污染这些判定。
    user_text: str = ""

    # -- 便捷访问（避免各层到处 getattr） --

    @property
    def plain_text(self) -> str:
        return getattr(self.message_context, "plain_text", "") or ""

    @property
    def group_id(self) -> str:
        return str(getattr(self.message_context, "group_id", "") or "")

    @property
    def user_id(self) -> str:
        return str(getattr(self.message_context, "user_id", "") or "")

    @property
    def actor(self) -> str:
        return getattr(self.message_context, "actor", "") or self.user_id

    @property
    def bot(self) -> Any:
        return getattr(self.message_context, "bot", None)

    @property
    def raw_event(self) -> Any:
        return getattr(self.message_context, "raw_event", None)

    def feature(self, key: str, default: bool = True) -> bool:
        """读取功能开关（走群功能覆盖）。"""
        if self.config is None:
            return default
        try:
            from ..group_features import is_feature_enabled

            gid = self.group_id or None
            return is_feature_enabled(self.config, gid, key)
        except Exception:  # noqa: BLE001
            return default

    @property
    def env_text(self) -> str:
        """已渲染的环境文本片段（供 PromptBuilder 拼接）。"""
        return "\n".join(
            str(v) for v in self.environment.values() if v
        )

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id or None,
            "trigger": self.trigger_reason or None,
            "interrupt": self.interrupt,
            "private": self.private_chat,
            "trace_id": self.trace_id or None,
        }


@dataclass
class AgentState:
    """单轮执行的运行时状态。

    **不持久化**（第七节明确要求）。它只在一次 `runtime.run()` 内存在，
    方法结束后即可丢弃。
    """

    messages: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    start_time: float = 0.0
    retry_count: int = 0
    budget: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def record_call(self, call: ToolCall) -> None:
        self.tool_calls.append(call)

    def record_result(self, result: ToolResult) -> None:
        self.tool_results.append(result)

    def results_for(self, tool_name: str) -> list[ToolResult]:
        return [r for r in self.tool_results if r.tool_name == tool_name]

    def first_result(self, tool_name: str) -> ToolResult | None:
        for result in self.tool_results:
            if result.tool_name == tool_name:
                return result
        return None

    def append_message(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


@dataclass
class AgentResult:
    """Agent 执行的最终结果。

    关键：**不再暴露 card_result / mute_result / draw_result ... 8 个字段**
    （第八节明确要求）。上层通过 `tool_results` 按工具名查询。
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=TokenUsage)
    steps: int = 0
    stop_reason: StopReason = StopReason.FINISHED
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- 按工具名查询（替代旧的 8 个独立字段） --

    def first_result(self, tool_name: str) -> ToolResult | None:
        for result in self.tool_results:
            if result.tool_name == tool_name:
                return result
        return None

    def results_for(self, tool_name: str) -> list[ToolResult]:
        return [r for r in self.tool_results if r.tool_name == tool_name]

    def output_of(self, tool_name: str, default: Any = None) -> Any:
        """取某工具的结构化输出。"""
        result = self.first_result(tool_name)
        if result is None or not result.success:
            return default
        return result.data.get("value", result.output)

    def succeeded(self, tool_name: str) -> bool:
        result = self.first_result(tool_name)
        return bool(result and result.success)

    def was_called(self, tool_name: str) -> bool:
        return any(c.tool_name == tool_name for c in self.tool_calls)

    @property
    def searched(self) -> bool:
        """是否触发了检索（保留旧 ChatResult.searched 语义）。"""
        return self.succeeded("search") or bool(
            self.metadata.get("searched", False)
        )

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "stop_reason": self.stop_reason.value,
            "tools": [c.tool_name for c in self.tool_calls],
            "reply_len": len(self.text or ""),
            **self.token_usage.as_log_fields(),
        }


__all__ = ["AgentContext", "AgentState", "AgentResult"]
