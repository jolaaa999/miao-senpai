# 架构审计与改进（2026-10）

审计对象：`E:\PROJECT\QQBot`（喵学姐 / miao-senpai）。
对照物：AstrBot（`AstrBotDevs/AstrBot`，AGPL-3.0）。

> **许可边界（必须先说清）**：
> AstrBot 是 **AGPL-3.0**，本项目根目录**没有 LICENSE 文件**（未声明许可）。
> 因此本轮所有"借鉴"都只到**设计思路**层面：读它为什么这么分层，
> 不复制任何代码、不引入它的依赖、不引入它的运行时。
> 若将来要对外分发本项目，需要先补一份 LICENSE；
> 一旦与 AGPL 代码有实际代码级复用，整个分发物都要按 AGPL 处理。

---

## 1. 真实架构（读完代码后的版本，不是文档里的版本）

### 1.1 一条消息的完整路径

```
OneBot V11 (NapCat)
  ↓  gateway_message_matcher  priority=1
MessageContext            统一事件模型（含 ASR 文本）
  ↓  Runtime.dispatch
Observers                 旁路采集（贴纸/近期聊天/进退群），**永不短路**
  ↓
Router                    三阶段：command → plugin → agent
  ↓  RouteResult.handled / consumed 分离
AgentTriggerPolicy        要不要进 Agent（@、私聊、关键词、打断）
  ↓
批处理窗口
  ↓  legacy_bridge.handle_agent_message
SessionExecutor           每会话 FIFO、reject-newest、max_queue_size=8
  ↓
AgentEntry                解析模式 LEGACY / SHADOW / CANARY / RUNTIME
  ↓  AgentRuntime.run()
ToolRegistry.visible_tools  权限第一层（提示期可见性）
  ↓
PromptBuilder → ModelGateway → SenpaiLLM.chat
  ↓  marker 协议 / 原生 tool_calls
ToolExecutor              权限第二层（执行期授权）+ 超时 + 记账
  ↓
AgentResult → ResponsePipeline → send_text_reply → _record_turn
```

### 1.2 已有的、值得保护的资产

| 资产 | 位置 | 为什么不能动 |
| --- | --- | --- |
| Command 优先、Observer 旁路 | `core/router.py` | 命令永远比 Agent 快且确定；观察者短路会导致"贴纸收集吞掉消息"这类事故 |
| 单一 Tool 执行采集点 | `agent/tools/executor.py` | 所有工具调用只有一条路，观测/权限/超时才可能完备 |
| Tool 元数据 | `agent/tools/registry.py::ToolDefinition` | 已含 permission/timeout/continuation/failure_policy/side_effect/recovery_policy，比 AstrBot 的 `FunctionTool` 厚 |
| 两层权限 | `visible()` + `authorize()` | 提示期不给看、执行期再拦一次，缺一不可 |
| 模式边界 | `agent/mode.py` + `entry.py` | SHADOW/CANARY 是上线期的安全阀 |
| MemoryFacade | `agent/memory_facade.py` | 记忆访问唯一入口，边界测试在守 |
| 143 个测试文件 | `tests/` | 大量是"阶段边界契约"，改架构时它们是唯一刹车 |

### 1.3 审计发现的真实问题（按严重度）

| # | 问题 | 证据 | 影响 |
| --- | --- | --- | --- |
| 1 | ~~`LEGACY` 模式是**假回滚**~~ **已修复** | `LegacyOrchestrator.run()` 必抛（`LegacyRunState` 从未 import）；`legacy_state.py` / `legacy_context.py` 零生产引用 | 出事时切 `AGENT_MODE=legacy` 并不能回到旧路径。**2026-10 已删除三个模块 + 把旧入口改成显式抛错的桩**，见 §5.5 |
| 2 | telemetry 采集完整但**默认不落地** | `agent/entry.py` 绑了 `RequestTelemetry`，只有 `DL_SENPAI_VALIDATION=1` 才写 JSONL | 线上排查时手里没有可 grep 的东西（本轮已修） |
| 3 | `llm.py::SenpaiLLM.chat()` 约 600 行单函数 | `llm.py` 466–1089 | 混了 provider 解析、本地 3B 特化、流式选择、历史裁剪。改动风险高 |
| 4 | 根目录 83 个平铺文件 / 35k 行 | `src/plugins/dl_senpai/*.py` | 认知负担；`web_search.py` 2015 行、`pptx_design.py` 1704 行 |
| 5 | 无 inbound MCP | 见 §3 | 无法复用生态里现成的工具 |
| 6 | 无 Skills 层 | 见 §3 | "怎么做事"和"能做什么"混在系统提示里 |
| 7 | 根目录无 LICENSE | — | 对外分发时有法律不确定性 |

---

## 2. AstrBot 值得借鉴的设计（基于 GitHub 仓库与官方文档，非二手博客）

### 2.1 可借鉴且**本轮已采纳**

| 设计 | AstrBot 出处 | 本项目怎么落地 |
| --- | --- | --- |
| 内向 MCP 接入 | `astrbot/core/provider/func_tool_manager.py` + `mcp_client.py` | `mcp_setup.py` + `agent/tools/mcp_tool.py`。但**没有**照搬它的 `_PermissionGuardedTool` 代理：本项目 `ToolDefinition` 自带 permission，再包一层就是重复 |
| 声明式扩展装载 | `astrbot/core/star/`（插件注册命令/工具/钩子） | `core/extensions.py::ExtensionRegistry`：一次登记命令/工具/路由/观察者，按 owner 一次摘净 |
| 请求级可观测 | dashboard 的会话/调用链视图 | `agent.request_summary` 一行日志（比 UI 早做，成本低得多） |

### 2.2 可借鉴但**本轮未做**（见 §6 P1/P2）

| 设计 | AstrBot 出处 | 为什么先不做 |
| --- | --- | --- |
| Skills 层（任务手册） | `astrbot/core/skills/skill_manager.py` + `SKILL.md`/`references/`/`scripts/` | 需要先有提示词分层改造，否则只是给系统提示再加一段 |
| SubAgent / handoff | `astrbot/core/agent/handoff.py`、`transfer_to_<name>`（v4.14 实验性） | 本项目是单 Agent + 工具，加子 Agent 等于加一层调度与上下文隔离，收益要先证明 |
| 上下文压缩器 | `astrbot/core/agent/context/` | 本项目已有 `_trim_history_for_local` 等局部裁剪，但**没有统一压缩器**；做之前要先量化 token 分布 |
| Cron 任务 | `astrbot/core/cron/manager.py` | 本项目已有自己的定时（主动聊天/热搜/刷帖），换框架无收益 |

---

## 3. 能力差距矩阵（诚实版：能"已有"的不算差距）

| 能力 | AstrBot | 喵学姐（本轮前） | 喵学姐（本轮后） | 判定 |
| --- | --- | --- | --- | --- |
| 多平台接入 | 16+ adapter（`core/platform/sources/*`） | OneBot V11 + NapCat | 同 | **不算差距**：单一平台是产品定位，不是缺陷 |
| 消息流水线阶段化 | `stage_order.py` 九阶段 | Router 三阶段 + Observer | 同 | **不算差距**：本项目 stage 更少但短路规则更明确（命令优先） |
| 插件体系 | `core/star/` | `core/plugins/` + owner 归属 | + Extension 声明式装载 | **差距收窄** |
| 工具元数据 | `FunctionTool`：name/description/parameters/handler/active/is_background_task | `ToolDefinition`：19 个字段 | 同 | **本项目更强**，不需要"对齐" |
| 工具权限 | 有（代理层） | 两层（可见 + 授权） | 同 | **本项目更强** |
| 原生 tool calling | 有 | 有（`NATIVE_TOOLS`） | 同 | **不算差距** |
| 标准 RAG | `core/knowledge_base/` 普通检索 | `knowledge_auto` 预取 | 同 | **不算差距**（只是名字不同） |
| Agentic RAG | 知识库检索工具 | `knowledge` 工具 | 同 | **不算差距** |
| **内向 MCP** | 有 | **无** | **有** | **本轮补齐**（P0） |
| Skills 任务手册 | 有 | 无 | 无 | 真差距（P1） |
| SubAgent / handoff | 有（实验性） | 无 | 无 | 真差距但低优先（P2） |
| 上下文压缩器 | 有 | 局部裁剪 | 局部裁剪 | 真差距（P2） |
| 定时任务 | `cron/manager.py` | 自有定时（主动聊天/热搜/刷帖） | 同 | **不算差距** |
| Web 控制台 | Vue dashboard | Go + Vue 控制台（日志/记忆/人设/） | 同 | **差距在颗粒度**：缺 trace/验证视图（P2） |
| 请求级 telemetry | 有 | 有采集、**默认不落地** | 有采集 + 一行摘要 | **本轮补齐**（P0） |
| 真正的旧路径回滚 | N/A | `legacy_orchestrator` 是死的 | 同 | 真差距（P1，见 §6） |

---

## 4. 明确不值得引入的 AstrBot 能力

| 能力 | 为什么不引入 |
| --- | --- |
| 多平台 adapter 抽象 | 本项目只有 OneBot 一个入口，抽象层没有第二个实现来验证，只会变成"为了扩展性而扩展性"。真要接第二个平台时再抽，那时才有两个真实样本 |
| 九阶段流水线（`stage_order.py`） | 现有三阶段 + "命令优先、观察者不短路"的规则已经覆盖同样语义。照搬会把 `RouteResult` 的 handled/consumed 语义打散，而那是 20+ 个测试在守的契约 |
| `FunctionTool` 式薄工具模型 | 本项目 `ToolDefinition` 已经更厚。向薄的看齐 = 主动丢能力 |
| `_PermissionGuardedTool` 代理层 | 本项目权限分布在 `visible()`（提示期）与 `PermissionService.authorize()`（执行期）。再包一层代理 = 第三处权限来源，三处必然漂移 |
| AstrBot 的 `star` 插件加载机制 | 本项目插件是 nonebot 生态内的 Python 模块，已有 `PluginRuntime` 做 owner 归属与停用。换成 star 式 = 重写插件运行时 |
| Dashboard 整套前端 | 本项目已有控制台。缺的只是 trace 视图，写一个 handler 比移植前端便宜一个数量级 |
| AGPL 代码本身的任何复用 | 见文首许可边界 |

---

## 5. 本轮实际完成的优化

五件事，都是"低风险 + 高价值"，且**没有改动任何既有公共接口的签名**。

### 5.1 内向 MCP 接入（P0，新能力）

- 新增 `mcp_setup.py`：按服务器起常驻 asyncio actor，持有连接、消费调用队列；
  单台失败只记录；启动不阻塞主流程；关闭时**先摘注册表再断连接**。
- 新增 `agent/tools/mcp_tool.py`：把 MCP 的 tool spec 翻译成 `ToolDefinition`。
  工具名归一化成 OpenAI 安全的 `mcp__<server>__<tool>`（≤64 字符，保住服务器前缀），
  schema 强制 `type: object`（否则整批 tools 会被接口拒）。
- 权限、超时、幂等、观测**全部继承**现有 `ToolExecutor`，一行没改。
- 默认关；`MCP_TOOLS_ADMIN_ONLY=true`；工具一律标 `side_effect=True`。

### 5.2 声明式扩展装载（P0，基础设施）

- 新增 `core/extensions.py::ExtensionRegistry`：一次登记 Command / Tool / Route / Observer，
  owner 统一取 `extension.id`，`remove(id)` 一次摘净（含观察者——
  `Router.unregister_owner` 只清 routes，观察者另有入口，这里两个都调）。
- `Runtime.extensions` + `Runtime.register_extension()` + `SERVICE_EXTENSIONS`。
- **没有**把现有 8 个生产工具迁到 Extension：那批 owner 语义被 20+ 个测试钉住，
  迁移是纯风险无收益。Extension 只服务新能力。

### 5.3 请求级 trace 摘要（P0，可观测性）

- `agent/entry.py::_log_request_summary()`：每条请求结束输出一行 
  `agent.request_summary`，含 trace_id / mode / reply_by / stop_reason / 各段耗时 / 
  队列等待 / 模型调用次数 / token / 工具名+结果+耗时 / 能力集 / 错误。
- **只写元数据**：不写用户原话、不写回复正文、不写记忆内容。
- 它回答的问题是：这条消息进了哪条路、为什么停、调了什么、谁最终生成的回复。
- 一个真实踩到的坑：字段名最初叫 `tokens`，被 `log_event` 的脱敏规则
  （含 `token` 的子串一律 `<redacted>`，那条规则是给 API Key 用的）吞掉，
  改成 `usage` 才落地 —— 有测试守着。

### 5.4 工具来源可见（P1，顺手）

- `ToolDefinition.source`（新字段，加在 dataclass **末尾**，避免位置构造错位）+
  `effective_source`（显式 → `plugin:<owner>` → `builtin`）。
- `ToolRegistry.capability_summary(context)`：一次问清"这轮模型能看见几个、
  注册了几个、来自哪些来源、几个不可逆"。trace 摘要里直接用。

### 5.5 删除旧编排死代码（P1 提前做掉）

- 删除 `agent/legacy_orchestrator.py`(1158) + `legacy_state.py`(116) + `legacy_context.py`(109)
  = **1383 行**死代码；三者均无（或仅有测试）引用。
- `handlers._handle_chat_inner` 改成**显式抛错**的桩（`legacy_path_removed`），
  而不是一起删掉：`legacy_bridge` 仍把它当 legacy_handler 注入，
  `AgentEntry._run_legacy` 靠捕获它走降级分支。让它消失会让 legacy 模式
  变成"没有 handler"，反而失去降级路径 —— 保留成"明确抛错"更安全。
- `agent/legacy_adapter.py` **保留**：它是 ToolCall → ChatResult 的形状转换，
  `llm.py` 生产路径仍在用。
- 语义修正：`AGENT_MODE=legacy` 不再宣称"回滚"，而是"按 runtime 跑 + 打一条
  deprecated warning"；枚举值保留（旧 `.env` 里有这个值，删值会让启动校验失败）。
- 测试面：删除 40 个只针对已删模块内部结构的断言（phase 6.5/6.6 的"拆 god method /
  阶段化"契约随文件一起作废），并把 10 个仍有意义的改写成**新契约**——
  最关键是 `test_legacy_mode_degrades_to_runtime`：配成 legacy + 旧入口抛错时
  必须仍然给出回复。另加"模块必须不存在"的反向断言，防止再塞假回滚入口。

### 5.6 配置与文档

- `config.py` 新增 8 个字段（全部带 alias，默认值保守）：
  `mcp_enable` / `mcp_servers` / `mcp_config_path` / `mcp_init_timeout` /
  `mcp_tool_timeout` / `mcp_max_tools` / `mcp_tools_admin_only` /
  `mcp_disabled_servers`，以及 `agent_trace_summary`。
- `.env.example` 补全说明；新增 `docs/mcp-inbound.md`、本文。
- 新增 36 个测试（`test_mcp_bridge.py` / `test_extension_registry.py` / 
  `test_trace_summary.py`）。全量 `2545 passed, 1 skipped`（基线 2509，+36）。

---

### 5.7 控制台「调用链」页（P2 提前做掉）

- 后端 `admin/backend/internal/services/traces.go`：读 `data/logs/bot-*.log`，
  解析 `agent.request_summary` 行为结构化行（手写 key=value 解析器，
  正确跳过引号内的空格与 `=`），支持 session/keyword/reply_by/mode/limit 过滤。
- 路由 `GET /x7k9-dl-senpai-console/v1/agent-traces`（只读、无状态）。
- 前端 `AgentTracesView.vue` + 侧栏「调用链」：慢请求(≥8s)/兜底/带错误高亮，
  行可展开看原始日志行。
- **为什么读日志而不是建表**：那行摘要本来就是"一条消息的完整结论"，
  再落库存一份就是第二份真相，还要处理保留期与一致性问题。
  代价是历史随日志轮转消失 —— 可接受，因为排查的是"刚刚那条为什么这样回"。
- 验证：`go build ./...` 通过；`go test ./internal/services/` 3 个用例通过；
  `vue-tsc -b` + `vite build` 通过（新增 chunk `AgentTracesView-*.js`）。

### 5.8 `llm.py::chat()` 拆分（第一刀已落地）

`chat()` 原为 625 行单函数，混了七件事。**先写等价测试、再动刀**，本轮切出两段：

| 段 | 原位置 | 去向 | 产出 |
|---|---|---|---|
| 开关解析 + 预取 | 142 行 | `SenpaiLLM._prepare_turn()` | `_TurnPrep`（12 字段） |
| 出站后处理 | 204 行 | `SenpaiLLM._polish_reply()` | 处理后的正文 |

结果：`chat()` 从 625 行降到约 318 行，剩下的部分是"怎么调用模型 + 重试 + 收尾"，
与"这一轮能用什么""发出去前要不要修"彻底分开。

等价性判据：`tests/test_llm_reply_polish.py`（9 个用例）= 7 条产出快照
（安慰腔剥离 / 复盘腔 / 反 AI 味 / 按句截断 / 拆泡 / 原样透传 / 私聊预算）
+ 2 条**归属**守卫（`chat()` 里不得再出现 `grounding_prefetch` / `strip_comfort_clauses`；
`_prepare_turn` 必须持有插嘴禁用与本地模型开关逻辑）。

刻意保留：`_TurnPrep` 里的 `searched` / `search_brief_text` / `search_detail_level`
在下游"多轮补搜"里还会被改写，所以 `chat()` 拿到后立即解包为局部变量 ——
这样这次拆分对下游是零改动，而不是为了对象化去重写调用循环。

未切：多轮补搜 + 接地重试（约 150 行）状态耦合最重（同时改写 content / tool_calls /
searched / search_brief_text），要动得先引入可变状态对象，风险与收益不匹配，暂不做。

### 5.9 技能层（Skills）垂直试点：PPT 设计

形态定为「**一页说明书**」，不是第二个工具层：

| 决策 | 取值 | 理由 |
|---|---|---|
| 存储 | `data/skills/<name>/SKILL.md` + frontmatter | 手写、可 diff、不需要数据库 |
| 选择 | 确定性触发词匹配（大小写不敏感子串，按命中数排序） | 可回归、可解释、零额外模型调用 |
| 注入位置 | 本轮 `user_payload`（与知识库/联网/链接正文同构） | 不动 `persona.build_system_prompt`，不碰它的字节级等价测试 |
| 预算 | 1 个技能 × 1500 字（可配） | 两个领域规范同时上会互相打架 |
| 默认 | `DL_SENPAI_SKILL_ENABLE=false` | 新能力默认关，不改默认行为 |
| 失败 | `skill.failed` 日志 + 什么都不加 | 加载失败绝不能挡住回复 |

渐进披露：扫目录只解析 frontmatter，命中才读正文 → 「装 20 个技能」≠「每条消息多 20 页」。

明确不做（写进 `docs/skills.md`）：技能内执行工具、多步编排、技能互调、向量选技能、后台热编辑。

试点技能 `data/skills/ppt-design/SKILL.md`：结构先于美化 / 一页一个论点 / 两色系 / 字号下限 /
自查清单 / 收尾页要写「需要谁拍什么板」。验收口径是「问一次 PPT 就能看到 `skill.injected` 一行」。

### 5.10 MCP 端到端验收 + 技能层收尾（本轮补齐）

**MCP**：`tests/test_mcp_runtime_e2e.py`（3 个用例，真子进程）把之前缺的三段拼起来验了——
扩展装配路径、管理员可见性、真调用与真卸载。顺带确认工具来源是 `mcp:<server>`，
所以 `capability_summary` 能看出"这条能力是哪台服务器提供的"。
上线后一周的观察口径写进 `docs/mcp-inbound.md`（`mcp.ready` / `tool.result` / 失败率门槛）。

**技能层**：补两件收尾 ——

- 热重载：每次注入前比一次文件指纹（路径 + mtime_ns），改了才重扫。
  用 mtime 不用内容哈希：技能是手写小文件，哈希会把懒加载的意义抵消掉。
- 技能清单：`catalog_text()` 输出「名字：说明 [触发: …]」，启动时以 `skills.ready` 记一行日志；
  **仍不注入提示** —— 技能靠触发词进上下文，清单常驻是纯开销。

**顺手修掉一个真实故障**：`agent/tools/social_tools.py` 里 `execute_bili` 的 try 块缩进被写坏
（`if action == "inbox":` 后直接跟同级的 `try:`），并且 `bl.` / `hs.` / `WebLoginError`
三个名字没有导入，`BILI` 的 schema enum 里也漏了 prompt 里写明的 `inbox`。
这处坏了整条 `_finalize_result → high_risk → social_tools` 导入链，会让**回复根本生成不出来**；
已按原有结构还原并补上导入与 enum。

## 6. 未实施的 P1 / P2 / P3

### P1

| 项 | 为什么本轮不做 | 建议做法 |
| --- | --- | --- |
| ~~真 `LEGACY` 回滚路径~~ **已完成（选择删除）** | 已删除三个模块并改为显式抛错桩，见 §5.5 | 后续如需真回滚，只能回滚代码版本（版本级回滚本来就比模式开关更可靠） |
| `llm.py::chat()` 拆分 | 600 行单函数，混 provider 解析 / 本地 3B 特化 / 流式 / 历史裁剪 | 按"解析 / 组装 / 调用 / 重试"切四段，每切一段先加等价测试（对比新旧 `messages` 与最终结果） |
| 动态记忆简报移出系统提示 | 提示词是**实测调优过的生产资产**（注释里写着安慰率 22%→55% 这类测量），`PromptBuilder` 刻意委派给 `persona.build_system_prompt` 保证逐字一致，且 `test_phase4_prompt_registry.py` 断言等价 | 先建 A/B 协议（同一批消息双跑、人工打分），有数据再动 |

### P2

| 项 | 说明 |
| --- | --- |
| Skills 层 | 值得做，但要先定"技能文档放在哪、怎么按需注入、和系统提示的边界在哪"。先做 1 个垂直技能跑通再谈框架 |
| 统一上下文压缩器 | 先量化：一份真实长会话的 token 分布（系统提示 / 记忆简报 / 历史 / 工具结果各占多少），再决定压哪一段 |
| ~~控制台 trace 视图~~ **已完成** | 见 §5.7（读日志、只读接口、一页列表） |
| 把 8 个生产工具迁到 Extension | 纯收益是"登记方式统一"，风险是 owner 语义漂移。等有新工具要加时顺手做，不单独发起 |

### P3

- SubAgent / handoff。需要先证明"单 Agent 不够用"，目前没有证据。
- 多平台 adapter。需要第二个真实平台做样本。
- MCP 配置热重载。现在重启一次即可，收益不足。

---

## 7. 过度设计 / 重复抽象 / 兼容性风险自检

| 检查项 | 结论 |
| --- | --- |
| 有没有为了"架构漂亮"加抽象 | 没有。`Extension` 只被新能力使用，没有强制迁移；`side_effect_level` 做成**派生属性**而不是新字段，避免与 `side_effect` + `journal.IRREVERSIBLE_TOOLS` 三处来源 |
| 有没有第二套工具元数据 | 没有。MCP 工具直接产出 `ToolDefinition`，与内置工具同构 |
| 有没有第二个权限判断点 | 没有。MCP 用 `visibility` 谓词 + 既有 `PermissionService`，没引入 `CapabilityPolicy` 之类的新层 |
| 有没有第二套执行路径 | 没有。全部走 `ToolExecutor` |
| 公共接口签名有没有改 | 没有。`ToolDefinition` 只在**末尾**加字段；`Runtime` 只加字段与方法；`SERVICE_EXTENSIONS` 是新增常量 |
| 既有能力有没有删 | 没有 |
| 兼容性风险 | ① `mcp>=1.2.0,<2` 已是 optional extra，但**未安装时 import 会失败** → 已用"每台服务器失败隔离 + 顶层函数内 import"兜住，未装 = 少几个工具而不是启动崩；② `agent_trace_summary` 默认开，会增加每请求一行日志（可关）；③ `usage` 字段名避开脱敏规则（见 §5.3） |
| 测试覆盖 | 新增 36 个（含 1 个真 SDK stdio 冒烟）+ 删除 40 个随模块作废的结构断言；全量 2525 passed / 1 skipped |

