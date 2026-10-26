"""MCP 内向接入（MCP Server → ToolDefinition → ToolRegistry）的行为契约。

这一层最容易悄悄坏掉的地方不是"连不上"，而是**形状翻译**：
工具名不合规会被接口 400、schema 不是 object 会被整批拒收、
owner 写错会让停服时摘不掉工具。所以这里的断言全部盯这些点，
不去测真实 MCP 服务器（那需要外部进程与网络）。
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

from dl_senpai.agent.tools.mcp_tool import (
    MCP_NAME_MAX,
    build_mcp_tool,
    build_mcp_tools,
    normalize_schema,
    safe_tool_name,
)
from dl_senpai.agent.tools.models import Continuation, ToolCall
from dl_senpai.agent.tools.registry import FailurePolicy, ToolRegistry
from dl_senpai.mcp_setup import MCPBridge, MCPServerActor, parse_servers_config


# ================================================================
# 假 MCP 会话：只实现 bridge 真正用到的三个方法
# ================================================================


class _Tool:
    def __init__(self, name: str, description: str = "", schema: dict | None = None):
        self.name = name
        self.description = description
        self.inputSchema = schema or {"type": "object", "properties": {}}


class _Block:
    def __init__(self, text: str, type: str = "text"):
        self.text = text
        self.type = type


class _CallResult:
    def __init__(self, blocks: list[_Block], is_error: bool = False):
        self.content = blocks
        self.isError = is_error


class _Listed:
    def __init__(self, tools: list[_Tool]):
        self.tools = tools


class _Session:
    """最小可用的 ClientSession 替身。"""

    def __init__(self, tools: list[_Tool], *, fail_call: bool = False):
        self._tools = tools
        self._fail_call = fail_call
        self.calls: list[tuple[str, dict]] = []

    async def initialize(self) -> None:
        return None

    async def list_tools(self) -> _Listed:
        return _Listed(self._tools)

    async def call_tool(self, name: str, arguments: dict) -> _CallResult:
        self.calls.append((name, arguments))
        if self._fail_call:
            return _CallResult([_Block("boom")], is_error=True)
        return _CallResult([_Block(f"{name}<-{arguments}")])


def _factory(session: _Session):
    """把替身包成工厂；bridge 只要求"async with 得到 session"。"""

    @asynccontextmanager
    async def _open():
        yield session

    return _open


def _broken_factory(message: str = "connection refused"):
    @asynccontextmanager
    async def _open():
        raise RuntimeError(message)
        yield  # pragma: no cover - 仅为让函数成为 generator

    return _open


class _Cfg:
    """bridge 需要的那几个配置项（不用真实 PluginConfig，避免读盘）。"""

    def __init__(self, **kw: Any) -> None:
        self.mcp_enable = True
        self.agent_native_tools = True
        self.mcp_servers = ""
        self.mcp_config_path = ""
        self.mcp_init_timeout = 5.0
        self.mcp_tool_timeout = 5.0
        self.mcp_max_tools = 40
        self.mcp_tools_admin_only = True
        self.mcp_disabled_servers = ""
        self.agent_admin_qq = "10001"
        self.__dict__.update(kw)


class _Ctx:
    """AgentContext 的最小替身（只看 user_id / config）。"""

    def __init__(self, user_id: str, cfg: _Cfg):
        self.user_id = user_id
        self.actor = user_id
        self.config = cfg


# ================================================================
# 1. 工具名归一化
# ================================================================


def test_tool_name_is_openai_safe() -> None:
    """OpenAI 只接受 [a-zA-Z0-9_-] 且 ≤64 字符；不合规会让整包 tools 400。"""
    name = safe_tool_name("web.search", "get/最新的/结果")
    assert name.startswith("mcp__")
    assert len(name) <= MCP_NAME_MAX
    assert all(ch.isalnum() or ch in "_-" for ch in name)


def test_same_tool_on_two_servers_does_not_collide() -> None:
    """两台服务器都提供 search 时必须仍是两个不同工具。

    否则 ToolRegistry 按 name 覆盖注册，会**静默丢掉**其中一台的能力。
    """
    a = safe_tool_name("alpha", "search")
    b = safe_tool_name("beta", "search")
    assert a != b


def test_long_tool_name_keeps_server_prefix() -> None:
    """超长截断必须保住服务器前缀，否则跨服务器的同名工具会撞车。"""
    name = safe_tool_name("alpha", "x" * 200)
    assert name.startswith("mcp__alpha__")
    assert len(name) == MCP_NAME_MAX


# ================================================================
# 2. schema 规整
# ================================================================


def test_schema_defaults_to_object() -> None:
    """没有 inputSchema 时给空 object，不能给 None（接口会拒）。"""
    assert normalize_schema(None) == {"type": "object", "properties": {}}


def test_schema_forces_object_root() -> None:
    """顶层 type 不是 object 时必须改成 object（否则整批 tools 被拒）。"""
    out = normalize_schema({"type": "array", "properties": {"a": {}}})
    assert out["type"] == "object"
    assert out["properties"] == {"a": {}}


def test_schema_keeps_valid_required() -> None:
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    assert normalize_schema(schema)["required"] == ["q"]


# ================================================================
# 3. ToolDefinition 字段映射
# ================================================================


async def _noop_caller(name: str, args: dict) -> str:
    return f"{name}/{args}"


def test_definition_carries_mcp_metadata() -> None:
    tool = build_mcp_tool(
        server="web",
        spec=_Tool("search", "查资料", {"type": "object", "properties": {}}),
        caller=_noop_caller,
        timeout=7.0,
    )
    # 走原生 tool calling：不能有 marker，也不能往 prompt 里加段落
    assert tool.marker == ""
    assert tool.prompt_text == ""
    assert tool.parser is None
    assert tool.source == "mcp:web"
    assert tool.owner == "mcp:web"
    assert tool.continuation is Continuation.RETURN_TO_MODEL
    assert tool.failure_policy is FailurePolicy.RETURN_TO_MODEL
    assert tool.timeout == 7.0
    assert tool.permission == "mcp:web"
    # 第三方服务行为不可预知 → 一律按不可逆处理
    assert tool.has_side_effect() is True
    assert tool.side_effect_level == "irreversible"
    assert "MCP:web" in tool.description


async def test_executor_returns_success_result() -> None:
    tool = build_mcp_tool(server="web", spec=_Tool("search"), caller=_noop_caller)
    call = ToolCall(tool_name=tool.name, arguments={"q": "x"})
    result = await tool.executor(None, call)
    assert result.success is True
    assert "q" in result.output
    assert result.data["mcp_server"] == "web"
    assert result.data["mcp_tool"] == "search"


async def test_executor_swallows_errors() -> None:
    """工具执行器约定「永不抛异常」，MCP 侧抛错必须转成失败结果。"""

    async def _boom(name: str, args: dict) -> str:
        raise ValueError("bad input")

    tool = build_mcp_tool(server="web", spec=_Tool("search"), caller=_boom)
    result = await tool.executor(None, ToolCall(tool_name=tool.name))
    assert result.success is False
    assert "bad input" in result.error
    assert result.error_type == "ValueError"


async def test_executor_maps_timeout() -> None:
    async def _slow(name: str, args: dict) -> str:
        raise asyncio.TimeoutError()

    tool = build_mcp_tool(server="web", spec=_Tool("search"), caller=_slow)
    result = await tool.executor(None, ToolCall(tool_name=tool.name))
    assert result.success is False
    assert result.error_type == "timeout"


# ================================================================
# 4. 批量翻译的去重与上限
# ================================================================


def test_duplicate_names_are_reported_not_silently_dropped() -> None:
    specs = [_Tool("a.b"), _Tool("a b")]  # 归一化后同名（都变成 a_b）
    tools, skipped = build_mcp_tools(server="s", specs=specs, caller=_noop_caller)
    assert len(tools) == 1
    assert any(reason.startswith("duplicate_name:") for reason in skipped)


def test_max_tools_caps_registration() -> None:
    specs = [_Tool(f"t{i}") for i in range(5)]
    tools, skipped = build_mcp_tools(
        server="s", specs=specs, caller=_noop_caller, max_tools=2
    )
    assert len(tools) == 2
    assert "max_tools" in skipped


def test_empty_name_is_skipped() -> None:
    tools, skipped = build_mcp_tools(
        server="s", specs=[_Tool("")], caller=_noop_caller
    )
    assert tools == []
    assert "empty_name" in skipped


# ================================================================
# 5. 配置解析
# ================================================================


def test_config_accepts_mcp_servers_wrapper() -> None:
    """从 Cursor / AstrBot 拷过来的配置通常带 mcpServers 外壳。"""
    servers, err = parse_servers_config(
        '{"mcpServers": {"web": {"command": "python", "args": ["-m", "x"]}}}'
    )
    assert err == ""
    assert list(servers) == ["web"]


def test_config_skips_disabled_server() -> None:
    servers, err = parse_servers_config(
        '{"a": {"command": "x"}, "b": {"command": "y", "enabled": false}}'
    )
    assert err == ""
    assert list(servers) == ["a"]


def test_config_reports_bad_json() -> None:
    servers, err = parse_servers_config("{not json}")
    assert servers == {}
    assert "JSON" in err


# ================================================================
# 6. Bridge：装配 / 调用 / 拆除 / 失败隔离
# ================================================================


class _FakeRuntime:
    """只提供 tools 的运行时（没有 register_extension 时走直连注册分支）。"""

    def __init__(self, registry: ToolRegistry):
        self.tools = registry


async def _bridge_with(monkeypatch, cfg: _Cfg, servers: dict, sessions: dict) -> MCPBridge:
    """按 servers/sessions 拼一个 bridge（不碰真实传输层）。"""
    import dl_senpai.mcp_setup as mcp_setup

    monkeypatch.setattr(mcp_setup, "_config_snapshot", lambda: cfg)
    monkeypatch.setattr(mcp_setup, "parse_servers_config", lambda raw, path="": (servers, ""))
    monkeypatch.setitem(mcp_setup._TRANSPORTS, "stdio", lambda c: sessions[c["command"]]())
    return MCPBridge()


async def test_bridge_registers_and_unregisters(monkeypatch) -> None:
    cfg = _Cfg()
    session = _Session([_Tool("lookup", "查点东西")])
    servers = {"web": {"command": "web", "type": "stdio"}}
    sessions = {"web": _factory(session)}
    bridge = await _bridge_with(monkeypatch, cfg, servers, sessions)

    registry = ToolRegistry()
    registry.disable_permissions()  # 权限层另有专测，这里只看注册/摘除
    status = await bridge.start(runtime=_FakeRuntime(registry))

    assert status["state"] == "ready"
    assert status["tools"] == 1
    name = registry.names()[0]
    assert name.startswith("mcp__web__")

    # 真正走一次调用链：ToolDefinition.executor → actor → session
    tool = registry.get(name)
    result = await tool.executor(None, ToolCall(tool_name=name, arguments={"q": "hi"}))
    assert result.success is True
    assert session.calls == [("lookup", {"q": "hi"})]

    await bridge.stop()
    assert registry.names() == []


async def test_broken_server_does_not_kill_others(monkeypatch) -> None:
    """一台服务器连不上，其余服务器必须照常可用。"""
    cfg = _Cfg()
    servers = {
        "bad": {"command": "bad", "type": "stdio"},
        "good": {"command": "good", "type": "stdio"},
    }
    sessions = {
        "bad": _broken_factory(),
        "good": _factory(_Session([_Tool("ok")])),
    }
    bridge = await _bridge_with(monkeypatch, cfg, servers, sessions)
    registry = ToolRegistry()
    registry.disable_permissions()
    status = await bridge.start(runtime=_FakeRuntime(registry))

    assert "bad" in status["errors"]
    assert len(registry.names()) == 1
    await bridge.stop()


async def test_bridge_is_disabled_by_default(monkeypatch) -> None:
    cfg = _Cfg(mcp_enable=False)
    bridge = await _bridge_with(monkeypatch, cfg, {}, {})
    status = await bridge.start(runtime=_FakeRuntime(ToolRegistry()))
    assert status["state"] == "disabled"


async def test_bridge_refuses_without_native_tools(monkeypatch) -> None:
    """没有原生 tool calling 时 MCP 工具根本不可能被模型看见。

    与其注册一批不可用工具（还会污染能力集统计），不如拒绝启动并说清原因。
    """
    cfg = _Cfg(agent_native_tools=False)
    bridge = await _bridge_with(monkeypatch, cfg, {}, {})
    status = await bridge.start(runtime=_FakeRuntime(ToolRegistry()))
    assert status["state"] == "blocked_no_native_tools"


# ================================================================
# 7. 权限：默认只有管理员可见
# ================================================================


async def test_mcp_tools_are_admin_only_by_default(monkeypatch) -> None:
    cfg = _Cfg()
    servers = {"web": {"command": "web", "type": "stdio"}}
    sessions = {"web": _factory(_Session([_Tool("lookup")]))}
    bridge = await _bridge_with(monkeypatch, cfg, servers, sessions)
    registry = ToolRegistry()
    registry.disable_permissions()
    await bridge.start(runtime=_FakeRuntime(registry))
    tool = registry.get(registry.names()[0])

    # 普通群友：可见性谓词直接挡掉（模型根本看不到这个工具）
    assert tool.is_visible(_Ctx("20002", cfg)) is False
    # 管理员：放行
    assert tool.is_visible(_Ctx("10001", cfg)) is True
    # 关掉管理员限制后对所有人开放
    open_cfg = _Cfg(mcp_tools_admin_only=False)
    assert tool.is_visible(_Ctx("20002", open_cfg)) is True
    await bridge.stop()


async def test_runtime_disable_of_server_hides_tools(monkeypatch) -> None:
    """运行期按服务器名屏蔽，不必改整段 JSON。"""
    cfg = _Cfg(mcp_disabled_servers="web")
    servers = {"web": {"command": "web", "type": "stdio"}}
    sessions = {"web": _factory(_Session([_Tool("lookup")]))}
    bridge = await _bridge_with(monkeypatch, cfg, servers, sessions)
    registry = ToolRegistry()
    registry.disable_permissions()
    await bridge.start(runtime=_FakeRuntime(registry))
    tool = registry.get(registry.names()[0])
    assert tool.is_visible(_Ctx("10001", cfg)) is False
    await bridge.stop()


# ================================================================
# 8. Actor：连接失败要能被 start() 看见，而不是永久挂住
# ================================================================


async def test_actor_reports_connection_error() -> None:
    actor = MCPServerActor("x", _broken_factory("nope"), init_timeout=2.0)
    tools, error = await actor.start()
    assert tools == []
    assert "nope" in error
    await actor.stop()


async def test_actor_call_before_connect_raises() -> None:
    actor = MCPServerActor("x", _broken_factory(), init_timeout=1.0)
    await actor.start()
    with pytest.raises(RuntimeError):
        await actor.call("t", {})
    await actor.stop()

# ================================================================
# 9. 真 SDK 冒烟（不 mock）：验证 actor 的"同一任务内进出"是对的
# ================================================================


SERVER_SOURCE = """
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool()
def echo(text: str) -> str:
    return f"echo:{text}"


if __name__ == "__main__":
    mcp.run()
"""


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("mcp") is None,
    reason="未安装 mcp 依赖（optional extra）",
)
async def test_actor_against_real_sdk(tmp_path) -> None:
    """真连一次 stdio MCP Server。

    为什么留着这个"慢"测试：actor 的设计完全由 mcp 传输层的
    "anyio 上下文必须在同一任务内进出"这条约束决定。
    只测假工厂的话，把这条约束写错也照样绿。
    """
    import sys

    from dl_senpai.mcp_setup import _stdio_session

    script = tmp_path / "server.py"
    script.write_text(SERVER_SOURCE, encoding="utf-8")

    actor = MCPServerActor(
        "demo",
        lambda: _stdio_session(
            {"command": sys.executable, "args": [str(script)]}
        ),
        init_timeout=60.0,
        call_timeout=60.0,
    )
    tools, error = await actor.start()
    try:
        assert error == "", error
        assert [t.name for t in tools] == ["echo"]
        assert await actor.call("echo", {"text": "hi"}) == "echo:hi"
    finally:
        await actor.stop()
