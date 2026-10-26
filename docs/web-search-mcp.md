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
| `auto`（默认） | 有 Key/URL 则优先 Tavily / SearXNG，再试 `ddgs` → DuckDuckGo Instant → Bing |
| `bing` | 仅 Bing HTML（**质量最差**：中文长词会被拆词，只做兜底） |
| `duckduckgo` | `ddgs`（若已装）→ Instant → Bing |
| `searxng` | 自建/公共 SearXNG（需 `DL_SENPAI_SEARCH_SEARXNG_URL`） |
| `tavily` | Tavily API（需 `DL_SENPAI_SEARCH_TAVILY_API_KEY`） |

实测结论（2026-10 在本机跑过）：

- `ddgs` 默认 `auto` 后端会挨个试 mojeek/yandex，实测 8.8 秒后整体超时；
  代码现在**指名 `backend="google"`**（约 3 秒，中文质量更好），失败才退回 `auto`。
- Bing HTML 抓取对中文长词会拆词（搜「原神 最新版本」返回「原」字词典条目），
  因此排到最后；`_merge_hits` 还会按域名多样性与转载关系收敛结果。
- 公共 SearXNG 实例实测 12 个，**0 个可用**（429 / 403 / 只给 HTML）。
  想用 SearXNG 必须自建，见下文。

可选增强（中文结果更稳）：

```bash
python -m pip install ddgs
```

### 结果质量相关的改动（2026-10）

| 项 | 现在的做法 |
|---|---|
| 正文抽取 | 优先 `trafilatura`，抽不到才退回内置正则。实测天气站从「半屏导航」变成真正的预报数据 |
| 转载去重 | 标题字符 3-gram 重叠系数 ≥ 0.8 判为转载，同稿不再占多个名额 |
| 总预算 | `DL_SENPAI_SEARCH_DEADLINE`（默认 15 秒）：首轮吃 80% → 换词/时效补搜 → 正文保底 2.5 秒 |
| 时效判定 | 摘要里出现当前年份也算「有时效线索」，避免明明是新内容还白补搜一轮 |
| 状态类问题 | 天气/汇率/油价/比分等不做「X 最新」补搜（换回的都是同义结果，纯耗时） |
| 失败重试 | `ddgs` 首次失败隔 0.3 秒重试一次，并用 `wait_for` 封住线程挂死 |

### 正文抓取与兜底（2026-10 实测）

搜索结果本身没问题，**不准确的根源是正文抓不到**：模型只看到 60~350 字的搜索摘要，
摘要里没有的装备名/数值/技能等级它就会自己补上。实测各站点：

| 站点 | 静态抓取 | 浏览器兜底 |
|------|----------|------------|
| 游民星空 / 游侠网 / 墨迹天气 | ✓ 1~8s（抖动大） | — |
| B站专栏 | ✗（SPA 空壳） | ✓ 2~3s |
| 知乎 | ✗ 403 | ✗ 风控（给 23 秒也是「请求存在异常」） |
| 百度贴吧 | ✗ 403 | ✗ 滑块验证 |
| NGA | ✗ 403 | ✗ |

所以现在的策略是：

- `DL_SENPAI_SEARCH_BROWSER_FALLBACK=true`：静态抓不到正文时用 Playwright 渲染一次（复用 `link_reader._browser_render`）。
- **只有详情类问题才动用浏览器**（`needs_page_detail`：配装/参数/攻略/数值/教程这类词）。普通问题不值得多等。
- **连浏览器也过不去的站点直接跳过**（`_HARD_BLOCKED_HOSTS`），不浪费那 9 秒。
- 没抓到正文的条目在 prompt 里标「仅搜索摘要，未见正文」，并在段首硬性要求：摘要里没写的数值/装备/等级不许自己补。
- 正文补全超时不再整批丢弃：先抓到的先用，超时那段退回摘要。

### 自建 SearXNG（已在本机部署）

```powershell
# 启动 / 重建（Docker Desktop 需要在运行）
pwsh -File scripts/start-searxng.ps1
# 停止
pwsh -File scripts/start-searxng.ps1 -Stop
```

- 容器名 `senpai-searxng`，端口 **8090**（8080 被本机某个 CEF 调试端口占用，换端口才通），
  配置在 `searxng/settings.yml`（开 JSON 接口、关限流、随机 `secret_key`）。
- `.env` 里对应 `DL_SENPAI_SEARCH_SEARXNG_URL=http://127.0.0.1:8090`。
- 容器带 `--restart unless-stopped`，Docker 起来后会自动拉起。

接入后的效果（本机实测）：单查询 5~6 秒返回、34~47 条多引擎结果（google cse + duckduckgo + brave），
正文抓取正常；比只跑 `ddgs` 时的 6~16 秒且结果抖动稳定得多。

**主源优先策略**：一旦配了 `SEARXNG_URL` 或 Tavily Key，`auto` 只问主源，
够 3 条就收工；主源空手才退回 `ddgs`/Bing 兜底链（避免拆词结果稀释好结果）。

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
