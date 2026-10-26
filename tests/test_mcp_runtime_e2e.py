"""MCP 端到端：真 MCP Server → ToolRegistry → 权限可见性 → 真调用。

`test_mcp_bridge.py` 覆盖的是**形状翻译**（工具名/schema/owner），用假会话跑得快；
但真实部署里还有三段只有拼起来才会出问题：

    1. 装配路径：走 `ExtensionRegistry.apply()` 还是直连 `registry.register()`；
    2. 可见性：MCP 工具默认只对管理员可见，普通群友 `visible_tools` 里不能出现；
    3. 真进程：stdio 子进程真的起得来、真的能返回文本、停服后真的能摘干净。

所以这一个文件是"慢测试"：它真的 spawn 一个 `python -m mcp` 风格的子进程。
慢是刻意的——只测假会话的话，上面三条任何一条写错都不会红。
"""

from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from dl_senpai.agent.tools.models import ToolCall
from dl_senpai.agent.tools.registry import ToolRegistry
from dl_senpai.core.extensions import ExtensionRegistry
from dl_senpai.mcp_setup import MCPBridge

pytestmark = pytest.mark.skipif(
    __import__("importlib").util.find_spec("mcp") is None,
    reason="未安装 mcp 依赖（optional extra）",
)


# 最小 MCP Server：一个 echo 工具就够了，我们要验的是链路不是功能。
SERVER_SOURCE = """
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("demo")


@mcp.tool()
def echo(text: str) -> str:
    return f"echo:{text}"


if __name__ == "__main__":
    mcp.run()
"""


class _Cfg:
    """bridge 需要的配置项（不用真实 PluginConfig，避免读盘与 .env 干扰）。"""

    def __init__(self, **kw: Any) -> None:
        self.mcp_enable = True
        self.agent_native_tools = True
        self.mcp_servers = ""
        self.mcp_config_path = ""
        self.mcp_init_timeout = 60.0
        self.mcp_tool_timeout = 60.0
        self.mcp_max_tools = 40
        self.mcp_tools_admin_only = True
        self.mcp_disabled_servers = ""
        self.agent_admin_qq = "10001"
        self.__dict__.update(kw)


class _Ctx:
    """AgentContext 的最小替身（可见性只看 user_id 与 config）。"""

    def __init__(self, user_id: str, cfg: _Cfg) -> None:
        self.user_id = user_id
        self.actor = user_id
        self.config = cfg


class _Runtime:
    """生产装配路径的最小替身：`tools` + `register_extension`。

    真实 `core/runtime.py` 上就是这两个入口被 `MCPBridge` 用到，
    所以这里只要能复现"扩展登记 → 工具进注册表"就够了。
    """

    def __init__(self, registry: ToolRegistry) -> None:
        self.tools = registry
        self.extensions = ExtensionRegistry(self)

    def register_extension(self, extension: Any) -> Any:
        return self.extensions.apply(extension)


async def _bridge_with_real_server(tmp_path: Any, monkeypatch: Any) -> tuple[MCPBridge, _Cfg]:
    """起一个真的 stdio MCP Server，并让 bridge 通过**真实配置解析**连它。"""
    import dl_senpai.mcp_setup as mcp_setup

    script = tmp_path / "server.py"
    script.write_text(SERVER_SOURCE, encoding="utf-8")

    cfg = _Cfg(
        mcp_servers=json.dumps(
            {"demo": {"command": sys.executable, "args": [str(script)], "type": "stdio"}}
        )
    )
    monkeypatch.setattr(mcp_setup, "_config_snapshot", lambda: cfg)
    bridge = MCPBridge()
    return bridge, cfg


async def test_mcp_tool_end_to_end(tmp_path, monkeypatch) -> None:
    """真 Server → 扩展装配 → 管理员可见 → 真调用 → 停服摘干净。"""
    bridge, cfg = await _bridge_with_real_server(tmp_path, monkeypatch)
    registry = ToolRegistry()
    registry.disable_permissions()  # 权限层单独测，这里只看装配与执行
    runtime = _Runtime(registry)

    status = await bridge.start(runtime=runtime)
    try:
        assert status["state"] == "ready", status
        assert status["tools"] == 1, status
        names = registry.names()
        assert len(names) == 1 and names[0].startswith("mcp__demo__")
        name = names[0]

        # 走的是 Extension 装配路径（生产路径），扩展 id 必须登记在案
        assert "mcp:demo" in runtime.extensions.ids()

        tool = registry.get(name)
        assert tool is not None
        # 来源精确到服务器名：排障时要能看出这条能力是哪台 MCP 提供的
        assert tool.source == "mcp:demo", tool.source
        result = await tool.executor(None, ToolCall(tool_name=name, arguments={"text": "hi"}))
        assert result.success is True, result
        assert "echo:hi" in (result.output or "")
    finally:
        await bridge.stop()

    # 停服后：扩展摘除 + 工具消失（否则模型还能看到一个已失效的工具）
    assert "mcp:demo" not in runtime.extensions.ids()
    assert registry.names() == []


async def test_mcp_tool_is_admin_only_in_visible_tools(tmp_path, monkeypatch) -> None:
    """端到端复验权限：普通群友看不到（模型根本不知道该调它）。"""
    bridge, cfg = await _bridge_with_real_server(tmp_path, monkeypatch)
    registry = ToolRegistry()
    runtime = _Runtime(registry)

    status = await bridge.start(runtime=runtime)
    try:
        assert status["state"] == "ready", status
        admin_visible = [t.name for t in registry.visible_tools(_Ctx("10001", cfg))]
        member_visible = [t.name for t in registry.visible_tools(_Ctx("20002", cfg))]
        assert any(n.startswith("mcp__demo__") for n in admin_visible), admin_visible
        assert not any(n.startswith("mcp__demo__") for n in member_visible), member_visible
    finally:
        await bridge.stop()


async def test_capability_summary_counts_mcp_source(tmp_path, monkeypatch) -> None:
    """能力摘要要能看出"这些工具来自 mcp"，否则排障时无法区分内建与第三方。"""
    bridge, cfg = await _bridge_with_real_server(tmp_path, monkeypatch)
    registry = ToolRegistry()
    registry.disable_permissions()
    runtime = _Runtime(registry)

    await bridge.start(runtime=runtime)
    try:
        summary = registry.capability_summary(_Ctx("10001", cfg))
        assert summary["visible"] >= 1
        assert "mcp:" in summary["sources"], summary  # 形如 "builtin:1,mcp:1"
    finally:
        await bridge.stop()
