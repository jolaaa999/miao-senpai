"""DummyTool：Phase 4 的**扩展性验收工具**。

用途（第四十五节）：证明新增一个工具**只需要注册 ToolDefinition**，
不需要修改：

    persona.py
    handlers.py
    llm.py
    agent/runtime.py

如果这个测试做不到，说明 Tool Registry 仍然是伪抽象。

DummyTool 只存在于测试路径，不注册进生产注册表。
"""

from __future__ import annotations

from typing import Any

from .models import Continuation, ToolCall, ToolResult, ToolCallSource
from .registry import ToolDefinition, ToolRegistry

# 注意：这些文本是**新增工具自带**的，不是改 persona 得来的
DUMMY_MARKER = "DUMMY"
DUMMY_PROMPT_TEXT = """
【示例工具】
想用示例工具时，在回复末尾另起一行写：
<<<DUMMY 参数>>>
群友看不到这一行。没有需要就别写。
""".strip()


def _parse_dummy(text: str) -> tuple[str, list[ToolCall]]:
    """解析 <<<DUMMY xxx>>>，支持 >>> / >> / ＞＞＞ / 】 容错闭合。"""
    import re

    close = r"(?:>>>|>>|＞＞＞|＞＞|】】|】>|】)"
    pattern = re.compile(rf"<<<DUMMY\s*([^>\n]*?)\s*{close}", re.IGNORECASE)
    calls: list[ToolCall] = []
    for match in pattern.finditer(text or ""):
        calls.append(
            ToolCall(
                tool_name="dummy",
                arguments={"value": match.group(1).strip()},
                raw=match.group(0),
                source=ToolCallSource.MARKER,
                marker=DUMMY_MARKER,
            )
        )
    cleaned = pattern.sub("", text or "").strip()
    return cleaned, calls


def _execute_dummy(context: Any, call: ToolCall) -> ToolResult:
    """纯函数执行器：不产生任何副作用。"""
    return ToolResult(
        tool_name="dummy",
        tool_call_id=call.id,
        success=True,
        output=f"dummy executed with {call.arguments.get('value', '')}",
        data={"value": call.arguments.get("value", "")},
    )


def build_dummy_tool() -> ToolDefinition:
    """构造 DummyTool 定义。"""
    return ToolDefinition(
        name="dummy",
        description="测试用示例工具",
        marker=DUMMY_MARKER,
        prompt_text=DUMMY_PROMPT_TEXT,
        parser=_parse_dummy,
        executor=_execute_dummy,
        continuation=Continuation.RETURN_TO_MODEL,
        visibility=lambda ctx: True,
        owner="test_dummy",
        priority=999,
        parameter_schema={
            "type": "object",
            "properties": {"value": {"type": "string"}},
        },
    )


def register_dummy_tool(registry: ToolRegistry) -> ToolDefinition:
    """把一个 DummyTool 注册进注册表。"""
    return registry.register(build_dummy_tool())


__all__ = [
    "build_dummy_tool",
    "register_dummy_tool",
    "DUMMY_MARKER",
    "DUMMY_PROMPT_TEXT",
]
