"""MCP 工具的 ToolDefinition 适配器。

职责**严格限定**为「形状翻译」：

    MCP Server 的工具描述（name / description / inputSchema）
        ↓  build_mcp_tools()
    ToolDefinition（可直接进 ToolRegistry，被 Agent 看见与执行）

**不负责**连接生命周期（连接 / 重连 / 关闭都在 mcp_setup.py）。

三个刻意的设计选择：

1. **不写 marker**。MCP 工具走原生 function calling（tools 参数），正文里
   没有任何 <<<...>>> 可解析。因此 marker 为空（解析层不会去碰它），
   prompt_text 也为空（不往 system prompt 塞新段落——prompt 是生产资产，
   新增 MCP 工具不该让它漂移）。

2. **不出现 if tool_name == ... 分支**。所有差异都由这里产出的 ToolDefinition
   字段承载，ToolExecutor 只认定义、不认工具名。

3. **保守的副作用判定**。MCP 背后是第三方服务，我们无法知道它会不会发消息、
   扣钱、删数据。因此一律 side_effect=True：一旦它执行过，本轮就不会再触发
   任何"重放"式兜底。宁可少一层自动补救，也不要重复提交一次外部操作。
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Awaitable, Callable

from .models import Continuation, ToolCall, ToolResult
from .registry import FailurePolicy, ToolDefinition

# 工具名前缀。加前缀是为了让「这条调用来自 MCP」在日志与模型侧一眼可辨，
# 同时天然避免与内置工具（card / mute / search…）重名。
MCP_NAME_PREFIX = "mcp__"

# OpenAI function name 上限 64 字符，且只允许 [a-zA-Z0-9_-]。
MCP_NAME_MAX = 64

# MCP 工具的解析优先级：排在内置工具（10~100）之后。
# 它不参与 marker 解析，这个值只影响注册表里的稳定排序与日志顺序。
MCP_PRIORITY = 500

_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")

# 一次调用的执行器签名：(原始工具名, 参数) -> 结果文本
ToolCaller = Callable[[str, dict[str, Any]], Awaitable[str]]


def _slug(text: str) -> str:
    """把任意文本压成 OpenAI 允许的函数名字符集。"""
    return _UNSAFE.sub("_", str(text or "").strip()) or "x"


def safe_tool_name(server: str, tool: str, *, max_len: int = MCP_NAME_MAX) -> str:
    """生成合法且可读的工具名：mcp__<server>__<tool>。

    截断策略：**保住服务器前缀，截短工具名**。
    反过来（截掉前缀）会让不同服务器的同名工具撞成一个名字，
    而 ToolRegistry 是按 name 覆盖注册的——那等于随机丢工具。
    """
    head = f"{MCP_NAME_PREFIX}{_slug(server)}__"
    room = max(1, max_len - len(head))
    return (head + _slug(tool)[:room])[:max_len]


def normalize_schema(input_schema: Any) -> dict[str, Any]:
    """把 MCP 的 inputSchema 规整成 OpenAI 能吃的 JSON Schema。

    只做「缺什么补什么」，不改写结构：本项目历史上在 parameter_schema 里
    写过人类可读简写，结果被兼容接口直接 400。这里宁可给一个空对象 schema，
    也不猜测参数含义。
    """
    schema = input_schema if isinstance(input_schema, dict) else {}
    out = dict(schema)
    if out.get("type") != "object":
        # 顶层必须是 object，否则 OpenAI 兼容接口会拒收整个 tools 参数
        out["type"] = "object"
    if not isinstance(out.get("properties"), dict):
        out["properties"] = {}
    if not isinstance(out.get("required"), list):
        out.pop("required", None)
    return out


def _pick(spec: Any, *names: str, default: Any = "") -> Any:
    """从 dict 或对象上按候选字段名取值（兼容 mcp.types.Tool 与裸 dict）。"""
    for name in names:
        if isinstance(spec, dict):
            if name in spec and spec[name] is not None:
                return spec[name]
        else:
            value = getattr(spec, name, None)
            if value is not None:
                return value
    return default


def _describe(server: str, description: str, *, max_len: int = 400) -> str:
    """给模型的工具说明：加服务器前缀 + 截断。

    前缀是**必需**的：不同 MCP 服务器常有同名工具（search / read / write），
    模型需要知道自己在调谁家的；这同时是排查时的第一线索。
    """
    body = " ".join(str(description or "").split())
    if len(body) > max_len:
        body = body[: max_len - 1] + "…"
    return f"[MCP:{server}] {body}".strip()


def build_mcp_tool(
    *,
    server: str,
    spec: Any,
    caller: ToolCaller,
    timeout: float = 30.0,
    allowed: Callable[[Any], bool] | None = None,
    owner: str = "",
    priority: int = MCP_PRIORITY,
) -> ToolDefinition:
    """把单个 MCP 工具描述翻译成 ToolDefinition。

    Args:
        server: MCP 服务器名（用于工具名前缀、权限策略与日志）。
        spec: MCP 返回的工具描述（对象或 dict）。
        caller: 真正发起调用的可调用对象，由 mcp_setup 提供。
        timeout: 执行超时（由 ToolExecutor 统一 enforce）。
        allowed: 可见性谓词；None 表示不额外限制。
        owner: 归属者，默认 mcp:<server>，用于整台服务器一键摘除。
    """
    raw_name = str(_pick(spec, "name", default="") or "").strip()
    name = safe_tool_name(server, raw_name)
    description = _describe(server, str(_pick(spec, "description", default="")))
    schema = normalize_schema(
        _pick(spec, "inputSchema", "input_schema", "parameters", default={})
    )
    owner_id = owner or f"mcp:{server}"

    async def _execute(context: Any, call: ToolCall) -> ToolResult:
        """执行一次 MCP 调用。**永不抛异常**（与 ToolExecutor 的约定一致）。

        超时单独标记 error_type：上层据此区分「工具挂了」与「工具慢」。
        """
        started = time.perf_counter()
        try:
            output = await caller(raw_name, dict(call.arguments or {}))
        except asyncio.CancelledError:
            # 取消必须传播，不能被吞成一个普通失败——
            # 吞掉会让「会话被中断」表现为一次正常收尾。
            raise
        except asyncio.TimeoutError:
            return ToolResult(
                tool_name=call.tool_name,
                tool_call_id=call.id,
                success=False,
                error=f"MCP 工具 {raw_name} 超时",
                error_type="timeout",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception as exc:  # noqa: BLE001 —— 第三方服务什么错都可能抛
            return ToolResult(
                tool_name=call.tool_name,
                tool_call_id=call.id,
                success=False,
                error=f"{type(exc).__name__}: {exc}",
                error_type=type(exc).__name__,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        return ToolResult(
            tool_name=call.tool_name,
            tool_call_id=call.id,
            success=True,
            output=str(output or ""),
            duration_ms=(time.perf_counter() - started) * 1000,
            data={"mcp_server": server, "mcp_tool": raw_name},
        )

    return ToolDefinition(
        name=name,
        description=description,
        # 无 marker / 无 prompt 文本：走原生 tools 参数，不改动 system prompt
        marker="",
        prompt_text="",
        executor=_execute,
        parser=None,
        parameter_schema=schema,
        permission=f"mcp:{server}",
        timeout=float(timeout or 0.0),
        rate_limit=0.0,
        requires_confirmation=False,
        # 结果必须回给模型：MCP 工具的价值就在「查回来接着说」
        continuation=Continuation.RETURN_TO_MODEL,
        failure_policy=FailurePolicy.RETURN_TO_MODEL,
        visibility=allowed,
        owner=owner_id,
        enabled=True,
        priority=priority,
        # 第三方服务行为不可预知 → 一律按不可逆处理，禁用失败重放
        side_effect=True,
        source=f"mcp:{server}",
    )


def build_mcp_tools(
    *,
    server: str,
    specs: list[Any],
    caller: ToolCaller,
    timeout: float = 30.0,
    allowed: Callable[[Any], bool] | None = None,
    max_tools: int = 0,
) -> tuple[list[ToolDefinition], list[str]]:
    """批量翻译，返回 (工具定义, 被跳过的原因)。

    去重规则：同一台服务器里若两个工具名归一化后撞车（例如 a.b 与 a-b），
    保留先出现的那个并记录跳过原因——**静默覆盖**会让某台服务器的能力
    无声消失，那种问题极难从现象反推。
    """
    tools: list[ToolDefinition] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for spec in specs or []:
        if not str(_pick(spec, "name", default="")).strip():
            skipped.append("empty_name")
            continue
        tool = build_mcp_tool(
            server=server,
            spec=spec,
            caller=caller,
            timeout=timeout,
            allowed=allowed,
        )
        if tool.name in seen:
            skipped.append(f"duplicate_name:{tool.name}")
            continue
        if max_tools and len(tools) >= max_tools:
            skipped.append("max_tools")
            break
        seen.add(tool.name)
        tools.append(tool)
    return tools, skipped


__all__ = [
    "MCP_NAME_PREFIX",
    "MCP_NAME_MAX",
    "MCP_PRIORITY",
    "normalize_schema",
    "safe_tool_name",
    "build_mcp_tool",
    "build_mcp_tools",
]
