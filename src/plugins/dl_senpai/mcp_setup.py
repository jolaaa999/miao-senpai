"""MCP 接入：把外部 MCP Server 的工具接进 ToolRegistry（内向 MCP）。

与项目里既有的 mcp_servers/ 方向**相反**，必须先分清：

    mcp_servers/*            →  把学姐自己的能力**导出**给 Cursor（外向）
    本模块（mcp_setup）      →  把外部 MCP Server 的工具**导入**给 Agent（内向）

两者共用同一套业务模块，但一个是 server、一个是 client，互不替代：
外向的那些是"别人来调学姐"，本模块是"学姐去调别人"。

架构位置（与 knowledge_setup 同构，放在 agent/ 之外）：

    MCP Server
        ↓  connection (stdio / streamable-http / sse)
    MCPServerActor（每台服务器一个 asyncio 任务）
        ↓  mcp_tool.build_mcp_tools()
    ToolDefinition  →  ToolRegistry  →  Agent

**为什么不直接替换现有 Tool System**：MCP 只是一个新的「工具来源」。
工具仍然经过同一套 Registry / PermissionService / ToolExecutor / Journal，
因此权限、幂等、超时、观测全部自动继承，一行都不用改。

**为什么每台服务器一个独立 asyncio 任务**：mcp 的客户端传输层用的是
anyio task group，"进入"与"退出"必须在**同一个任务**里发生。
若在主协程里 with 再在别处 close，会抛 cancel scope 冲突。
用一个常驻任务 + 队列（actor 模式）就同时满足了：
连接常驻、调用可并发、关闭时在同一任务内收尾。
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .observability.logging import log_event

# 单次调用等待上限（秒）。比 ToolDefinition.timeout 略宽，
# 让 ToolExecutor 那一层先超时（它才会写进 telemetry），
# 这里只是防止 future 永远挂着。
DEFAULT_CALL_TIMEOUT = 60.0
# 单台服务器的初始化上限：连不上就跳过，不能拖住整个机器人启动。
DEFAULT_INIT_TIMEOUT = 30.0


# ================================================================
# 配置解析
# ================================================================


def parse_servers_config(raw: str = "", *, path: str = "") -> tuple[dict[str, dict], str]:
    """解析 MCP 服务器配置，返回 (servers, 错误说明)。

    支持两种写法，与 Cursor / AstrBot 的 mcp.json 兼容：

        {"web-search": {"command": "python", "args": ["-m", "x"]}}
        {"mcpServers": {"web-search": {...}}}     ← 多包一层的也认

    为什么同时认两层：用户从别处拷配置过来时几乎总带着 mcpServers 外壳，
    要求他手工拆开是纯粹的摩擦，而这个拆解只多五行。
    """
    text = (raw or "").strip()
    source = "DL_SENPAI_MCP_SERVERS"
    if not text and path:
        try:
            text = Path(path).expanduser().read_text(encoding="utf-8")
            source = path
        except OSError as exc:
            return {}, f"读取 MCP 配置文件失败: {exc}"
    if not text:
        return {}, ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"{source} 不是合法 JSON: {exc}"
    if not isinstance(data, dict):
        return {}, f"{source} 顶层必须是对象"
    inner = data.get("mcpServers")
    if isinstance(inner, dict):
        data = inner
    servers: dict[str, dict] = {}
    for name, cfg in data.items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("enabled") is False:
            continue
        servers[str(name)] = cfg
    return servers, ""


def _transport_of(cfg: dict) -> str:
    """判定传输方式。显式 type/transport 优先，其次按有无 url 猜。"""
    raw = str(cfg.get("type") or cfg.get("transport") or "").strip().lower()
    if raw in ("stdio", "command"):
        return "stdio"
    if raw in ("sse",):
        return "sse"
    if raw in ("http", "streamable_http", "streamable-http", "streamablehttp"):
        return "http"
    return "http" if cfg.get("url") else "stdio"


# ================================================================
# 会话工厂：把三套传输统一成「async with 得到 session」
# ================================================================


@asynccontextmanager
async def _stdio_session(cfg: dict) -> AsyncIterator[Any]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    command = str(cfg.get("command") or "").strip()
    if not command:
        raise ValueError("stdio 配置缺少 command")
    env = {}
    raw_env = cfg.get("env")
    if isinstance(raw_env, dict):
        # 只做字符串化：MCP 侧要求 dict[str, str]，数字会直接抛校验错
        env = {str(k): str(v) for k, v in raw_env.items()}
    params = StdioServerParameters(
        command=command,
        args=[str(a) for a in (cfg.get("args") or [])],
        env={**os.environ, **env} if env else None,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            yield session


@asynccontextmanager
async def _http_session(cfg: dict) -> AsyncIterator[Any]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    url = str(cfg.get("url") or "").strip()
    if not url:
        raise ValueError("http 配置缺少 url")
    headers = cfg.get("headers") if isinstance(cfg.get("headers"), dict) else None
    async with streamablehttp_client(url, headers=headers) as (read, write, _sid):
        async with ClientSession(read, write) as session:
            yield session


@asynccontextmanager
async def _sse_session(cfg: dict) -> AsyncIterator[Any]:
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    url = str(cfg.get("url") or "").strip()
    if not url:
        raise ValueError("sse 配置缺少 url")
    headers = cfg.get("headers") if isinstance(cfg.get("headers"), dict) else None
    async with sse_client(url, headers=headers) as (read, write):
        async with ClientSession(read, write) as session:
            yield session


_TRANSPORTS: dict[str, Callable[[dict], Any]] = {
    "stdio": _stdio_session,
    "http": _http_session,
    "sse": _sse_session,
}


def _render_result(result: Any) -> str:
    """把 MCP 的 CallToolResult 渲染成给模型看的纯文本。

    只取文本块：图片/资源类内容在这里转成一句占位说明。
    为什么不塞 base64 —— 那会瞬间吃光上下文预算，而模型也读不了。
    """
    blocks = getattr(result, "content", None)
    if not isinstance(blocks, list):
        return str(result or "")
    parts: list[str] = []
    for block in blocks:
        text = getattr(block, "text", None)
        if isinstance(text, str) and text.strip():
            parts.append(text)
            continue
        kind = getattr(block, "type", None) or type(block).__name__
        parts.append(f"（{kind} 内容，暂不支持展示）")
    body = "\n".join(parts).strip()
    if getattr(result, "isError", False):
        raise RuntimeError(body or "MCP 工具返回错误")
    return body


# ================================================================
# Actor：一台服务器一个常驻任务
# ================================================================


class MCPServerActor:
    """单台 MCP 服务器连接。

    用法：
        actor = MCPServerActor("web", factory)
        tools, error = await actor.start()
        text = await actor.call("search", {"q": "x"})
        await actor.stop()
    """

    def __init__(
        self,
        name: str,
        factory: Callable[[], Any],
        *,
        init_timeout: float = DEFAULT_INIT_TIMEOUT,
        call_timeout: float = DEFAULT_CALL_TIMEOUT,
    ) -> None:
        self.name = name
        self._factory = factory
        self._init_timeout = float(init_timeout)
        self._call_timeout = float(call_timeout)
        self._task: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._ready = asyncio.Event()
        self._session: Any = None
        self._tools: list[Any] = []
        self._error = ""
        self._closed = False

    @property
    def tools(self) -> list[Any]:
        return self._tools

    @property
    def error(self) -> str:
        return self._error

    async def start(self) -> tuple[list[Any], str]:
        """建立连接并取回工具清单。返回 (tools, error)。

        连接失败**不抛异常**：一台服务器挂掉不该让整台机器人起不来。
        """
        self._task = asyncio.create_task(self._run(), name=f"mcp-{self.name}")
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=self._init_timeout)
        except asyncio.TimeoutError:
            self._error = f"连接超时({self._init_timeout:g}s)"
            await self._cancel()
        return self._tools, self._error

    async def _run(self) -> None:
        """常驻循环：进入传输层 → 初始化 → 列工具 → 消费调用请求。

        进入与退出都在**本任务**内完成，这是 mcp 传输层的硬性要求。
        """
        try:
            async with self._factory() as session:
                await session.initialize()
                listed = await session.list_tools()
                self._tools = list(getattr(listed, "tools", listed) or [])
                self._session = session
                self._ready.set()
                while True:
                    item = await self._queue.get()
                    if item is None:
                        break
                    fut, tool_name, arguments = item
                    if fut.done():
                        continue
                    try:
                        result = await session.call_tool(tool_name, arguments)
                        if not fut.done():
                            fut.set_result(_render_result(result))
                    except Exception as exc:  # noqa: BLE001
                        if not fut.done():
                            fut.set_exception(exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 —— 任何连接期异常都只记录
            self._error = f"{type(exc).__name__}: {exc}"
        finally:
            self._session = None
            self._ready.set()  # 唤醒可能在等 start() 的调用方
            self._fail_pending(f"mcp server {self.name} 已断开")

    def _fail_pending(self, reason: str) -> None:
        """唤醒所有还在等结果的调用：连接没了，它们不该永远挂着。"""
        while not self._queue.empty():
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is None:
                continue
            fut = item[0]
            if not fut.done():
                fut.set_exception(RuntimeError(reason))

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """发起一次工具调用（可并发：请求进队列，结果走各自的 future）。"""
        if self._closed:
            raise RuntimeError(f"mcp server {self.name} 已关闭")
        if self._session is None:
            raise RuntimeError(
                f"mcp server {self.name} 未连接"
            )
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        await self._queue.put((fut, tool_name, dict(arguments or {})))
        return await asyncio.wait_for(fut, timeout=self._call_timeout)

    async def stop(self) -> None:
        """发送结束哨兵并等待任务退出（超时就取消）。"""
        self._closed = True
        if self._task is None:
            return
        try:
            self._queue.put_nowait(None)
        except Exception:  # noqa: BLE001
            pass
        try:
            await asyncio.wait_for(self._task, timeout=5.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            await self._cancel()
        except Exception:  # noqa: BLE001 —— 关闭期的异常不外抛
            pass
        self._task = None

    async def _cancel(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        self._task = None


# ================================================================
# Bridge：配置 → 连接 → 注册（一次装配，可整体拆除）
# ================================================================


def _config_snapshot() -> Any:
    """取当前配置；拿不到时返回 None（调用方按"未开启"处理）。"""
    try:
        from .config_runtime.runtime import get_config

        return get_config()
    except Exception:  # noqa: BLE001 —— 配置层不可用不该让 MCP 抛出去
        return None


def _allowed_predicate(server: str) -> Callable[[Any], bool]:
    """可见性谓词：MCP 工具对谁生效。

    **默认只有管理员可见**（mcp_tools_admin_only=True）。理由：
    MCP 服务器是第三方进程，能力边界未知；默认把它开给全群等于
    把一台不受控的执行器交给任何群友。想开放就显式关掉这个开关。
    """

    def _allowed(context: Any) -> bool:
        cfg = getattr(context, "config", None) or _config_snapshot()
        if cfg is None:
            return False
        if not bool(getattr(cfg, "mcp_enable", False)):
            return False
        # 运行时按服务器名单独关掉（热重载只改配置，不改注册表）
        disabled = str(getattr(cfg, "mcp_disabled_servers", "") or "")
        if server in {p.strip() for p in disabled.replace(";", ",").split(",")}:
            return False
        if not bool(getattr(cfg, "mcp_tools_admin_only", True)):
            return True
        try:
            from .agent.permissions import PermissionService

            return PermissionService(config=cfg).is_admin(context)
        except Exception:  # noqa: BLE001 —— 判不出身份就当没有权限
            return False

    return _allowed


class MCPBridge:
    """MCP 服务器群的装配与拆除。

    生命周期只有两件事：`start()` 一次、`stop()` 一次。
    中途的增删改**刻意不做**——配置热重载接 MCP 属于下一阶段，
    现在重启一次即可（启动期拉起，不阻塞主流程）。
    """

    def __init__(self) -> None:
        self._actors: dict[str, MCPServerActor] = {}
        self._extensions: list[str] = []
        self._errors: dict[str, str] = {}
        self._state = "idle"
        self._tool_count = 0
        self._lock: asyncio.Lock | None = None
        # 记住 start() 用的是哪个 runtime：stop() 必须用同一个。
        # 否则会去全局单例上摘工具，而工具其实注册在别处，
        # 表现为"停服后工具还在"，且从日志上完全看不出来。
        self._runtime: Any = None

    # -- 装配 --

    async def start(self, *, runtime: Any = None, force: bool = False) -> dict[str, Any]:
        """连接全部已配置服务器并注册其工具。返回状态摘要。"""
        cfg = _config_snapshot()
        if cfg is None:
            self._state = "no_config"
            return self.status()
        if not bool(getattr(cfg, "mcp_enable", False)):
            self._state = "disabled"
            return self.status()
        # 没有原生 tool calling 就没有任何办法让模型看见 MCP 工具：
        # marker 协议要求工具自带 marker，而第三方工具不可能有。
        # 与其注册一批模型根本看不见的工具（还会污染能力集统计），
        # 不如直接拒绝启动并把该改哪个开关写进日志。
        if not bool(getattr(cfg, "agent_native_tools", False)):
            self._state = "blocked_no_native_tools"
            log_event(
                "mcp.blocked",
                level="warning",
                reason="agent_native_tools_off",
                hint="MCP 工具只能通过原生 tool calling 暴露；"
                "请设置 DL_SENPAI_AGENT_NATIVE_TOOLS=true",
            )
            return self.status()

        servers, err = parse_servers_config(
            str(getattr(cfg, "mcp_servers", "") or ""),
            path=str(getattr(cfg, "mcp_config_path", "") or ""),
        )
        if err:
            self._state = "config_error"
            self._errors["_config"] = err
            log_event("mcp.config_error", level="error", error=err)
            return self.status()
        if not servers:
            self._state = "no_servers"
            log_event("mcp.no_servers", level="warning")
            return self.status()

        if runtime is None:
            try:
                from .core.runtime import get_runtime

                runtime = get_runtime()
            except Exception:  # noqa: BLE001
                runtime = None
        self._runtime = runtime

        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self._actors and not force:
                return self.status()
            await self._start_servers(cfg, servers, runtime)
        return self.status()

    async def _start_servers(
        self, cfg: Any, servers: dict[str, dict], runtime: Any
    ) -> None:
        from .agent.tools.mcp_tool import build_mcp_tools
        from .core.extensions import Extension

        init_timeout = float(getattr(cfg, "mcp_init_timeout", DEFAULT_INIT_TIMEOUT))
        call_timeout = float(getattr(cfg, "mcp_tool_timeout", DEFAULT_CALL_TIMEOUT))
        max_tools = int(getattr(cfg, "mcp_max_tools", 40) or 0)

        for name, server_cfg in servers.items():
            transport = _transport_of(server_cfg)
            factory = _TRANSPORTS.get(transport)
            if factory is None:
                self._errors[name] = f"不支持的传输方式: {transport}"
                continue
            actor = MCPServerActor(
                name,
                lambda cfg=server_cfg, f=factory: f(cfg),
                init_timeout=init_timeout,
                call_timeout=call_timeout,
            )
            self._actors[name] = actor
            specs, error = await actor.start()
            if error:
                # 单台失败只记录：其余服务器照常可用，
                # 一台坏配置不应该让整批能力一起消失。
                self._errors[name] = error
                log_event(
                    "mcp.server_failed",
                    level="warning",
                    server=name,
                    transport=transport,
                    error=error,
                )
                continue

            tools, skipped = build_mcp_tools(
                server=name,
                specs=specs,
                caller=actor.call,
                timeout=call_timeout,
                allowed=_allowed_predicate(name),
                max_tools=max_tools,
            )
            if not tools:
                log_event(
                    "mcp.server_no_tools",
                    level="warning",
                    server=name,
                    skipped=",".join(skipped),
                )
                continue

            extension_id = f"mcp:{name}"
            extension = Extension(
                id=extension_id,
                name=f"MCP {name}",
                description=f"MCP 服务器 {name} 提供的 {len(tools)} 个工具",
                source="mcp",
                tools=tools,
            )
            if runtime is not None and hasattr(runtime, "register_extension"):
                runtime.register_extension(extension)
            else:
                # 没有 Runtime（纯单测/CLI）时退回直接注册，
                # 保持"适配器可独立使用"的性质。
                registry = getattr(runtime, "tools", None)
                if registry is None:
                    from .agent.tools.registry import get_tool_registry

                    registry = get_tool_registry()
                for tool in tools:
                    registry.register(tool)
            self._extensions.append(extension_id)
            self._tool_count += len(tools)
            log_event(
                "mcp.server_ready",
                server=name,
                transport=transport,
                tools=len(tools),
                skipped=",".join(skipped) or None,
            )

        self._state = "ready" if self._tool_count else "connected_no_tools"
        log_event(
            "mcp.ready",
            servers=len(self._actors),
            tools=self._tool_count,
            failed=len(self._errors),
            errors=",".join(sorted(self._errors)) or None,
        )

    # -- 拆除 --

    async def stop(self) -> None:
        """关闭全部连接并摘除注册的工具。

        顺序很重要：**先摘注册表，再断连接**。反过来的话，
        模型可能在这中间拿到一个已失效工具并调用它。
        """
        runtime = self._runtime
        if runtime is None:
            try:
                from .core.runtime import get_runtime

                runtime = get_runtime()
            except Exception:  # noqa: BLE001
                runtime = None
            self._runtime = runtime

        extensions = getattr(runtime, "extensions", None) if runtime else None
        for extension_id in self._extensions:
            try:
                if extensions is not None:
                    extensions.remove(extension_id)
                else:
                    # 没有 ExtensionRegistry 时退回按 owner 摘：
                    # **必须用同一个 registry**（就是 start() 注册进去的那个），
                    # 否则会去全局单例上摘，而工具在别处，摘不干净。
                    registry = getattr(runtime, "tools", None)
                    if registry is None:
                        from .agent.tools.registry import get_tool_registry

                        registry = get_tool_registry()
                    registry.unregister_owner(extension_id)
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "mcp.unregister_failed",
                    level="warning",
                    extension=extension_id,
                    error=type(exc).__name__,
                )
        self._extensions.clear()

        for name, actor in list(self._actors.items()):
            try:
                await actor.stop()
            except Exception as exc:  # noqa: BLE001
                log_event(
                    "mcp.stop_failed",
                    level="warning",
                    server=name,
                    error=type(exc).__name__,
                )
        self._actors.clear()
        self._tool_count = 0
        self._state = "stopped"

    # -- 查询 --

    def status(self) -> dict[str, Any]:
        """状态摘要（供日志与后续控制台展示，不含任何用户/凭据信息）。"""
        return {
            "state": self._state,
            "servers": sorted(self._actors),
            "tools": self._tool_count,
            "errors": dict(self._errors),
        }


_bridge: MCPBridge | None = None


def get_mcp_bridge() -> MCPBridge:
    global _bridge
    if _bridge is None:
        _bridge = MCPBridge()
    return _bridge


def reset_mcp_bridge(bridge: MCPBridge | None = None) -> None:
    """替换全局 bridge（测试用）。"""
    global _bridge
    _bridge = bridge


async def start_mcp_tools(*, runtime: Any = None) -> dict[str, Any]:
    """启动入口（供插件 startup 调用）。异常一律吞掉。

    为什么吞掉：MCP 是**增强能力**，它起不来最多是少几个工具；
    让它把机器人启动搞崩是明确不划算的交易。
    """
    try:
        return await get_mcp_bridge().start(runtime=runtime)
    except Exception as exc:  # noqa: BLE001
        log_event(
            "mcp.start_failed",
            level="error",
            error=f"{type(exc).__name__}: {exc}",
        )
        return {"state": "error", "error": str(exc)}


async def stop_mcp_tools() -> None:
    """关闭入口（供插件 shutdown 调用）。"""
    try:
        await get_mcp_bridge().stop()
    except Exception:  # noqa: BLE001 —— 关闭期异常不外抛
        pass


__all__ = [
    "MCPServerActor",
    "start_mcp_tools",
    "stop_mcp_tools",
    "MCPBridge",
    "parse_servers_config",
    "get_mcp_bridge",
    "reset_mcp_bridge",
]
