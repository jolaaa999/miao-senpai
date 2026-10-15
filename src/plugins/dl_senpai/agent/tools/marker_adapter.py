"""ToolCallAdapter 接口 + MarkerToolAdapter 实现。

职责**严格限定**为：模型输出 → ToolCall 列表。**绝不执行业务**
（第二十节）。重构前 `parse_*` 与执行交织在 handlers 里，本层把它们分开。

    MarkerToolAdapter : text → [ToolCall]         （当前生产路径）
    NativeToolAdapter : tool_calls → [ToolCall]   （扩展点，未接管）

容错闭合（第十九节）必须完整保留。探查得到的真实差异：

| marker | >>> | >> | ＞＞＞ | 】 |
|---|---|---|---|---|
| CARD/MUTE/IMPRESSION | ✅ | ❌ | ❌ | ❌ |
| DRAW | ✅ | ✅ | — | ✅ |
| BROWSE | ✅ | ✅ | — | — |
| VOICE | ✅ | — | — | ✅ |
| SEARCH | ✅ | ✅ | ✅ | ✅ |
| STICKER | 仅带关键词时 | — | — | — |

这些差异**不是 bug，是历史调优结果**。Adapter 通过"委托给各模块现有的
parse_* 函数"来保证逐字等价——不重新实现正则。这是刻意的：
**重新实现就意味着可能引入差异**。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from .models import ToolCall, ToolCallSource
from .registry import ToolDefinition, ToolRegistry


class ToolCallAdapter(Protocol):
    """工具调用适配器接口（第四十六节）。"""

    def parse_response(self, text: str, context: Any = None) -> "ParseOutput":
        """把模型输出解析成工具调用。"""
        ...

    def render_tools(self, tools: list[ToolDefinition]) -> str:
        """把可见工具渲染成模型可读的说明文本。"""
        ...


@dataclass
class ParseOutput:
    """解析结果。

    Attributes:
        text: 剥掉 marker 之后的干净正文。
        calls: 解析出的工具调用（按原文位置排序）。
        searched: 是否触发了检索类工具（保留旧字段语义）。
    """

    text: str = ""
    calls: list[ToolCall] = None  # type: ignore[assignment]
    searched: bool = False

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    def calls_for(self, tool_name: str) -> list[ToolCall]:
        return [c for c in self.calls if c.tool_name == tool_name]

    def first(self, tool_name: str) -> ToolCall | None:
        for call in self.calls:
            if call.tool_name == tool_name:
                return call
        return None

    @property
    def has_calls(self) -> bool:
        return bool(self.calls)


class MarkerToolAdapter:
    """marker 文本协议适配器（当前生产路径）。

    实现策略：**逐个工具委托给它自己的 parser**，Adapter 本身不写正则。
    这样做的代价是每个工具要提供 `parser`；收益是零行为漂移。

    解析顺序按 tool.priority 排序 —— 保证多 marker 混合时结果稳定
    （第五十四节要求"解析顺序必须稳定"）。
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    # -- parse --

    def parse_response(self, text: str, context: Any = None) -> ParseOutput:
        """按优先级依次调用各工具的 parser，逐步剥离 marker。"""
        from ...observability.logging import log_event

        remaining = text or ""
        calls: list[ToolCall] = []
        searched = False
        # position 用于保持稳定顺序：以工具 priority 为序累加
        position = 0

        for tool in self._registry.parseable_tools():
            if tool.parser is None:
                continue
            try:
                new_text, tool_calls = tool.parser(remaining)
            except Exception as exc:  # noqa: BLE001 — 单个 parser 失败不影响其他
                log_event(
                    "tool.parse_failed",
                    level="warning",
                    tool=tool.name,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue

            if isinstance(new_text, str):
                remaining = new_text

            for call in tool_calls or []:
                # 补全 adapter 层负责的字段
                if not call.tool_name:
                    call.tool_name = tool.name
                call.marker = call.marker or tool.marker
                call.source = ToolCallSource.MARKER
                call.position = position
                position += 1
                if not call.id:
                    call.id = f"{call.tool_name}-{call.position}"
                calls.append(call)

            if tool.name == "search" and tool_calls:
                searched = True

        return ParseOutput(text=remaining, calls=calls, searched=searched)

    # -- render --

    def render_tools(self, tools: list[ToolDefinition]) -> str:
        """把工具说明渲染成 system prompt 片段。

        顺序按 priority，**文本内容原样取自 prompt_text**——不加工、不润色。
        """
        chunks: list[str] = []
        for tool in tools:
            body = (tool.prompt_text or "").strip()
            if body:
                chunks.append(body)
        return "\n".join(chunks)

    @staticmethod
    def marker_hint(tool: ToolDefinition) -> str:
        """生成该工具的 marker 提示（供工具自行拼进 prompt_text）。"""
        return f"<<<{tool.marker}>>>" if tool.marker else ""


class NativeToolAdapter:
    """OpenAI 原生 tool_calls 适配器。

    2026-10-12 起接管生产：`DL_SENPAI_AGENT_NATIVE_TOOLS=true` 时，
    runtime 会把 tool_schemas() 发给模型、并解析 tool_calls，不再依赖
    正文里的 `<<<MARKER>>>`。默认关闭，因此 marker 协议仍是默认路径。
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self._registry = registry

    def parse_response(self, text: str, context: Any = None) -> ParseOutput:
        """native 模式下 text 不含 marker，正文即 text，无 call。"""
        return ParseOutput(text=text or "", calls=[])

    def parse_native_calls(self, raw_calls: list[Any]) -> list[ToolCall]:
        """把 OpenAI 的 tool_calls 转换成统一 ToolCall。

        同时兼容 SDK 对象与 dict 两种输入，参数是 JSON 字符串时自行解析。
        供 `SenpaiLLM._finalize_result` 与 AgentRuntime 使用。
        """
        import json

        calls: list[ToolCall] = []
        for index, raw in enumerate(raw_calls or []):
            name = ""
            args: dict[str, Any] = {}
            call_id = ""
            try:
                fn = getattr(raw, "function", None) or (
                    raw.get("function") if isinstance(raw, dict) else None
                )
                if fn is not None:
                    name = getattr(fn, "name", None) or (
                        fn.get("name") if isinstance(fn, dict) else ""
                    ) or ""
                    raw_args = getattr(fn, "arguments", None) or (
                        fn.get("arguments") if isinstance(fn, dict) else None
                    )
                    if isinstance(raw_args, str) and raw_args.strip():
                        args = json.loads(raw_args)
                    elif isinstance(raw_args, dict):
                        args = raw_args
                call_id = str(getattr(raw, "id", "") or (
                    raw.get("id") if isinstance(raw, dict) else ""
                ) or "")
            except Exception:  # noqa: BLE001
                continue
            if not name:
                continue
            calls.append(
                ToolCall(
                    tool_name=name,
                    arguments=args,
                    id=call_id or f"{name}-{index}",
                    raw=str(raw)[:200],
                    source=ToolCallSource.NATIVE,
                    position=index,
                )
            )
        return calls

    def render_tools(self, tools: list[ToolDefinition]) -> str:
        """native 模式下工具通过 API 参数传递，不注入 system prompt。"""
        return ""

    @staticmethod
    def tool_schemas(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
        """生成 OpenAI tools 参数。

        注意：`parameter_schema` 必须是标准 JSON Schema。历史上文件/命令类
        工具这里写的是 {"path": "str"} 这种人类可读简写，直接发给兼容接口
        会被判 400，2026-10-12 已全部改为标准写法。
        """
        out: list[dict[str, Any]] = []
        for tool in tools:
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.parameter_schema
                        or {"type": "object", "properties": {}},
                    },
                }
            )
        return out


__all__ = [
    "ToolCallAdapter",
    "MarkerToolAdapter",
    "NativeToolAdapter",
    "ParseOutput",
]
