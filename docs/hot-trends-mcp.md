# 热梗 MCP（Cursor）

学姐机器人与 Cursor 共用同一套热榜采集逻辑（`dl_senpai.trends`）。

目录放在 `mcp_servers/`，避免与 PyPI 包名 `mcp` 冲突。

## 学姐侧

默认开启。环境变量见 `.env.example` 中 `DL_SENPAI_TRENDS_*`。

启动 bot 时会后台预热缓存；对话时把标题级热梗注入 system prompt，闲聊可轻度接梗。

## Cursor MCP

### 安装可选依赖

```bash
python -m pip install -e ".[mcp]"
```

依赖钉在 `mcp>=1.2,<2`（含 FastMCP）。若本机已装 mcp v2，请先降级：

```bash
python -m pip install "mcp>=1.2.0,<2"
```

### 配置示例（Cursor MCP）

在 Cursor 的 MCP 配置中加入（路径改成你的仓库绝对路径）：

```json
{
  "mcpServers": {
    "hot-trends": {
      "command": "python",
      "args": ["E:/PROJECT/QQBot/mcp_servers/hot_trends_mcp/server.py"],
      "env": {
        "DL_SENPAI_TRENDS_BASE_URL": "https://uapis.cn/api/v1/misc/hotboard",
        "DL_SENPAI_TRENDS_SOURCES": "weibo,douyin,bilibili,baidu,zhihu",
        "DL_SENPAI_TRENDS_CACHE_DIR": "E:/PROJECT/QQBot/data/dl_senpai/trends"
      }
    }
  }
}
```

### 工具

| 工具 | 作用 |
|------|------|
| `hot_trending` | 按平台查热榜（weibo/douyin/bilibili/baidu/zhihu） |
| `hot_summary` | 跨平台标题摘要（与学姐备忘同格式） |

若默认域名 DNS 失败，把 `DL_SENPAI_TRENDS_BASE_URL` 换成可访问的热榜接口；也可用 `DL_SENPAI_TRENDS_FALLBACK_BASES` 配置备用。当前默认是 `uapis.cn` 的 hotboard。
