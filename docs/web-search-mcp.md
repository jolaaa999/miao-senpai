# 联网检索 MCP（Cursor）

学姐机器人与 Cursor 共用同一套检索逻辑（`dl_senpai.web_search`）。

目录放在 `mcp_servers/`，避免与 PyPI 包名 `mcp` 冲突。

## 学姐侧

默认开启。有两层联网：

1. **自动联网**（`DL_SENPAI_SEARCH_AUTO=true`，默认开）：问天气/新闻/股价/「今天」「最新」等时效问题时，**调模型前**系统先搜好，把结果塞进上下文，不用你或模型写 `<<<SEARCH>>>`
2. **模型补搜**：自动结果不够时，模型仍可写 `<<<SEARCH 查询词>>>` 再搜一轮（最多 `DL_SENPAI_SEARCH_MAX_ROUNDS` 轮）

插嘴模式默认不联网（`DL_SENPAI_SEARCH_ALLOW_ON_INTERRUPT=false`）。

环境变量见 `.env.example` 中 `DL_SENPAI_SEARCH_*`。

### Provider

| 值 | 说明 |
|------|------|
| `auto`（默认） | 有 Key/URL 则优先 Tavily / SearXNG，再试 `ddgs` → Bing → DuckDuckGo Instant |
| `bing` | 仅 Bing HTML |
| `duckduckgo` | `ddgs`（若已装）→ Instant → Bing |
| `searxng` | 自建/公共 SearXNG（需 `DL_SENPAI_SEARCH_SEARXNG_URL`） |
| `tavily` | Tavily API（需 `DL_SENPAI_SEARCH_TAVILY_API_KEY`） |

可选增强（中文结果更稳）：

```bash
python -m pip install ddgs
```

## Cursor MCP

### 安装可选依赖

```bash
python -m pip install -e ".[mcp,search]"
```

`search` 额外组安装 `ddgs`，中文检索更稳。可选 Tavily Key 见 `.env` 中 `DL_SENPAI_SEARCH_TAVILY_API_KEY`。

### 一键配置（推荐）

本仓库已包含 **项目级** MCP 配置：`.cursor/mcp.json`

在 Cursor 打开本仓库后，到 **Settings → MCP** 确认已出现：

- `qqbot-web-search` — 实时联网搜索
- `qqbot-hot-trends` — 热榜摘要

若未自动加载，把下面内容合并进你的全局 MCP 配置（路径改成你的仓库绝对路径）：

```json
{
  "mcpServers": {
    "qqbot-web-search": {
      "command": "python",
      "args": ["E:/PROJECT/QQBot/scripts/run-mcp-web-search.py"]
    }
  }
}
```

启动脚本会自动读取仓库 `.env` / `.env.dev` 中的 `DL_SENPAI_SEARCH_*`。

### Agent Skill

项目技能：`.cursor/skills/qqbot-web-search/SKILL.md`

在对话里提到「联网搜」「最新新闻」「实时查询」时，Agent 会优先走 MCP 的 `web_search_realtime`。

### 手动配置（旧方式）

在 Cursor 的 MCP 配置中加入（路径改成你的仓库绝对路径）：

```json
{
  "mcpServers": {
    "web-search": {
      "command": "python",
      "args": ["E:/PROJECT/QQBot/mcp_servers/web_search_mcp/server.py"],
      "env": {
        "DL_SENPAI_SEARCH_PROVIDER": "auto",
        "DL_SENPAI_SEARCH_MAX_RESULTS": "5"
      }
    }
  }
}
```

可与热梗 MCP 同时配置，互不影响。

### 工具

| 工具 | 作用 |
|------|------|
| `web_search_realtime` | **推荐** — 带检索时间戳的文本摘要，适合时效问题 |
| `web_search` | 返回 JSON：title / url / snippet / source |
| `web_search_brief` | 返回适合喂给模型的文本摘要 |
