# MCP 内向接入：让学姐去调别人的工具

> 本文对应 `src/plugins/dl_senpai/mcp_setup.py` + `agent/tools/mcp_tool.py`。

## 1. 先分清方向

仓库里有两套完全不同的 MCP，名字一样但方向相反：

| 目录 | 方向 | 谁调谁 | 用途 |
| --- | --- | --- | --- |
| `mcp_servers/browser_mcp` 等 4 个 | **外向** | Cursor → 学姐的业务模块 | 让 IDE 里的 AI 能操作本项目的浏览器/热搜/搜索/店铺导入 |
| `mcp_setup.py`（本文） | **内向** | 学姐 → 外部 MCP Server | 让她用上别人写好的工具（数据库、内部 API、各种 SaaS） |

两者共用同一批业务模块，但一个是 server、一个是 client，互不替代。

## 2. 架构位置

```
MCP Server（stdio 子进程 / streamable-http / sse）
      ↓  每台一个 asyncio 常驻任务
MCPServerActor         连接、初始化、列工具、消费调用请求
      ↓  mcp_tool.build_mcp_tools()
ToolDefinition         与内置工具**完全同构**
      ↓  ExtensionRegistry（owner = mcp:<server>）
ToolRegistry → visible_tools → 模型
      ↓  模型发起调用
ToolExecutor           权限 / 超时 / 记账 / 副作用记录，全部继承
```

关键点：**MCP 只是一个新的「工具来源」**，不是新的执行路径。
工具仍然经过同一个 Registry、同一个 PermissionService、同一个 ToolExecutor、
同一份 Journal。所以权限、幂等、超时、可观测性都是自动继承的，一行都没改。

## 3. 为什么每台服务器一个常驻任务

MCP 的 Python 客户端传输层基于 anyio task group，"进入"和"退出"必须在
**同一个任务**里发生。若在主协程里 `async with`、在别处 `aclose()`，
会抛 cancel scope 冲突（`Attempted to exit cancel scope in a different task`）。

因此用 actor 模式：一个常驻任务持有连接，调用方只往队列里投递请求、
在自己的 future 上等结果。这样同时满足：

- 连接常驻（不用每次调用重新握手）
- 调用可并发（各自 future 独立）
- 关闭时在同一任务内收尾（先摘注册表，再退出传输层）

## 4. 配置

```dotenv
DL_SENPAI_AGENT_NATIVE_TOOLS=true      # 前置条件，见下
DL_SENPAI_MCP_ENABLE=true
DL_SENPAI_MCP_SERVERS={"web":{"command":"python","args":["-m","my_mcp"]}}
# 或者指向文件：DL_SENPAI_MCP_CONFIG=config/mcp.json
DL_SENPAI_MCP_MAX_TOOLS=40
DL_SENPAI_MCP_TOOLS_ADMIN_ONLY=true
```

`mcpServers` 外壳会被自动剥掉，所以从 Cursor / AstrBot 拷来的配置可以直接粘。

支持的传输方式（`type` 字段，缺省按有没有 `url` 猜）：

| type | 关键字段 |
| --- | --- |
| `stdio` | `command` / `args` / `env` |
| `http`（=streamable-http） | `url` / `headers` |
| `sse` | `url` / `headers` |

## 5. 为什么必须开 native tools

内置工具靠 **marker 协议**暴露：模型在文本里写 `[[SEARCH:...]]`，
框架解析出来执行。第三方工具不可能自带 marker，也没有 `prompt_text`，
因此只有**原生 tool calling**（OpenAI `tools` 参数）这条路能把它交给模型。

所以配置里只开 MCP 不开 `DL_SENPAI_AGENT_NATIVE_TOOLS` 时，
`mcp_setup` 会**直接拒绝启动**并打一条 `mcp.blocked`：
注册一批模型根本看不见的工具，只会污染能力集统计，是纯粹的负收益。

## 6. 安全默认值

| 默认 | 理由 |
| --- | --- |
| `MCP_ENABLE=false` | 会拉起第三方进程 / 连外部地址，必须显式选择 |
| `MCP_TOOLS_ADMIN_ONLY=true` | 能力边界未知的执行器，不该默认开放给全群 |
| 工具 `side_effect=True` | 第三方服务行为不可预知，一律按不可逆处理 |
| `MCP_MAX_TOOLS=40` | 防止一次接入几十个工具把上下文预算吃光 |
| 单服务器失败只记录 | 一台坏配置不该让整批能力一起消失 |

## 7. 排障

启动日志里这几行就够定位：

```
mcp.blocked        reason=agent_native_tools_off hint=...   # 前置条件没开
mcp.config_error   error=...                                # JSON 写错
mcp.server_failed  server=web error=...                     # 单台连不上
mcp.server_no_tools server=web skipped=...                  # 连上了但没工具
mcp.ready          servers=2 tools=17 failed=0              # 正常
```

调用侧看 `agent.request_summary` 那一行里的 `tools=mcp__web__search:success:812ms`。

## 8. 与 AstrBot 的关系

AstrBot 的 MCP 接入（`astrbot/core/provider/func_tool_manager.py`，
含 `_PermissionGuardedTool` 代理层）方向与本模块一致，
但它的 `FunctionTool` 元数据比本项目的 `ToolDefinition` 薄得多：
没有 timeout / continuation / side_effect / recovery_policy。
本项目把这些直接复用现有字段，没有再包一层代理对象。

## 端到端验收（已自动化）

`tests/test_mcp_runtime_e2e.py` 用**真的 stdio MCP Server 子进程**跑通整条链：

1. 真配置解析（`parse_servers_config` 不 mock）→ 2. `ExtensionRegistry.apply()` 装配（生产路径）
→ 3. `visible_tools` 权限过滤（管理员看得到 / 普通群友看不到）→ 4. 真调用拿到 `echo:hi`
→ 5. `stop()` 后扩展与工具都摘干净。

跑它：`python -m pytest tests/test_mcp_runtime_e2e.py -q`（约 15 秒，会 spawn 子进程）。

## 上线后观察一周（放开 admin-only 之前）

盯这三类日志行，一周后按数据决定要不要 `DL_SENPAI_MCP_TOOLS_ADMIN_ONLY=false`：

| 日志 | 含义 | 判断标准 |
|---|---|---|
| `mcp.ready servers=… tools=… failed=…` | 启动时是否连上 | `failed` 应恒为 0 |
| `tool.call tool=mcp__…` + 对应 `tool.result` | 真被调用 | 有调用才说明模型认得这些工具 |
| `tool.result` 里 `success=false` / `mcp.server_failed` | 失败率 | 失败率 > 30% 就别放开 |

另外看控制台「调用链」页：`tools=` 字段里出现 `mcp__` 前缀即说明这一轮用上了。

**为什么不一上来就全群开放**：MCP 服务器是第三方进程，能力边界（能不能读文件/发请求）
由对方决定，我们只能看到它声明的 schema。先限定主人可见，等于先自己踩一遍。
