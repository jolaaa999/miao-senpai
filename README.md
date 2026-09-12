# QQ 深度学习学姐机器人

基于 **NapCat（OneBot v11）+ NoneBot2** 的 QQ 群聊机器人。人设是热情可爱的深度学习学姐：群内 `@` 必回，未 `@` 时低频率自然插嘴；模型走你自己的 **OpenAI 兼容中转站**（默认模型名 `gpt-5.6`）。

## 架构

```
QQ 群 → NapCat → 反向 WebSocket → NoneBot2 → dl_senpai 插件 → 中转站 GPT
```

## 环境要求

- Python 3.12+
- 可用的中转站 `API Key` + `Base URL`
- [NapCat](https://github.com/NapNeko/NapCatQQ) + 机器人 QQ 号

## 快速开始

### 1. 安装依赖

```bash
cd QQBot
python -m pip install -e ".[dev]"
```

### 2. 配置环境变量

```bash
copy .env.example .env
```

编辑 `.env`，至少填写：

- `OPENAI_API_KEY`：中转站 Key
- `OPENAI_BASE_URL`：中转站地址（通常以 `/v1` 结尾）
- `OPENAI_MODEL`：模型 ID（确认中转站实际可用名，常见为 `gpt-5.6`）
- `ONEBOT_ACCESS_TOKEN`：与 NapCat 中 token 一致（建议设置）

可选：

| 变量 | 说明 | 默认 |
|------|------|------|
| `DL_SENPAI_INTERRUPT_PROB` | 插嘴概率 | `0.03` |
| `DL_SENPAI_INTERRUPT_COOLDOWN` | 插嘴冷却（秒） | `180` |
| `DL_SENPAI_ALLOWED_GROUPS` | 白名单群号，逗号分隔；空=全部 | 空 |
| `DL_SENPAI_ENABLE_PRIVATE` | 是否响应私聊 | `true` |
| `DL_SENPAI_TRENDS_ENABLE` | 注入近期热梗备忘 | `true` |
| `DL_SENPAI_TRENDS_BASE_URL` | 热榜 API | `https://uapis.cn/api/v1/misc/hotboard` |
| `DL_SENPAI_SEARCH_ENABLE` | 按需联网检索 | `true` |
| `DL_SENPAI_SEARCH_PROVIDER` | `auto` / `bing` / `duckduckgo` / `searxng` / `tavily` | `auto` |
| `DL_SENPAI_CHECKIN_GROUPS` | 签到功能白名单群号 | `980229149` |

热梗说明与 Cursor MCP 配置：[docs/hot-trends-mcp.md](docs/hot-trends-mcp.md)

联网检索与 Cursor MCP 配置：[docs/web-search-mcp.md](docs/web-search-mcp.md)（项目已含 `.cursor/mcp.json` + Agent Skill）

服装店商品爬取 / 识图导入 MCP：[docs/shop-import-mcp.md](docs/shop-import-mcp.md)

浏览器逛网站（淘宝 / Pixiv / 截图）：[docs/browser-mcp.md](docs/browser-mcp.md)

指定群签到指令：`签到` / `我的积分` / `积分排行` / `连续排行` / `今日任务` / `完成任务` / `签到帮助`

### 3. 启动 Bot（先于 NapCat）

```bash
python bot.py
```

默认监听 `http://127.0.0.1:27315`，OneBot 路径为 `/onebot/v11/ws`。

### 4. 配置 NapCat、进群、开机自启

- 对接 NapCat：[docs/napcat-setup.md](docs/napcat-setup.md)
- **进群 + 长期挂机（Windows）**：[docs/deploy-windows.md](docs/deploy-windows.md)

开机自启（注册计划任务，执行一次即可）：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\install-autostart.ps1
```

## 使用方式

- 群里 `@机器人` 提问（深度学习或闲聊均可）
- 不 `@` 时约 3% 概率插一句，带冷却，深度学习关键词会略提高概率
- 对话按群保留最近约 20 轮短期记忆（`data/dl_senpai/memory/`）
- 开启热梗后，学姐会参考近期微博/抖音/B站等热榜标题轻度接梗
- 开启联网后，问新闻/天气/股价等时效问题时会**实时检索**再回答（闲聊不会每句都搜）
- 开启浏览器后，可说「去淘宝找衣服」「pixiv 找壁纸」「截图这个网址」，学姐会真的打开网页并把图发到群里
- 指定群可「签到」：连续天数、积分、称号、每日任务、概率表情包

## 测试

```bash
python -m pytest -v
```

## 项目结构

```
bot.py
src/plugins/dl_senpai/   # 人设 / 记忆 / 触发 / LLM / 热梗 / 联网 / 消息处理
mcp_servers/hot_trends_mcp/   # Cursor 热梗 MCP
mcp_servers/web_search_mcp/   # Cursor 联网检索 MCP
mcp_servers/shop_import_mcp/  # Cursor 服装店导入 MCP
mcp_servers/browser_mcp/      # Cursor 浏览器逛网站 MCP
tests/
docs/napcat-setup.md
docs/hot-trends-mcp.md
docs/web-search-mcp.md
docs/shop-import-mcp.md
docs/browser-mcp.md
```

## 说明

- NapCat 登录与风控风险自负，建议使用小号。
- 本仓库不包含 NapCat 本体，也不存储你的 API Key（请勿把 `.env` 提交到 git）。

## 已知问题与改进（维护备忘）

### 禁言话术曾「复读 + 装傻」（2026-03）

现象：禁言后学姐反复说「已经帮你禁言了」；对方还能说话却说「发不了消息」；并硬往「有没有心事、来跟学姐说说」上引。

原因简要：
- 人设里禁言说明不够硬，模型爱复读上轮成功话术；
- 「发不了消息」与短时禁言现实不符（几十秒就解禁，且对方正在发言）；
- 小模型易滑向心理咨询腔；
- 辱骂也会触发「补写 MUTE」补救，被误当成「对方要求禁言」。

已做改进：
- `persona.py`：禁止倾诉腔；禁言口头规则改为「没写 <<<MUTE>>> 不许说禁了 / 对方在说话就别说发不了」；
- `persona.py`：主提示词改为「活人感」优先（先反应情绪、短句口语、禁止客服/咨询收尾、禁止复读开场白）；
- `style.py`：发送前清掉「发不了消息」与心事套话；本轮未真正禁言时清掉「已经帮你禁言了」；
- `handlers.py`：仅「明确要求禁言」才补救 MUTE；辱骂未禁成不再贴系统说明。

### 联网曾断开（已接回，2026-03）

群聊链路一度没挂上 `<<<SEARCH>>>` 检索。现已重新接入：默认 `DL_SENPAI_SEARCH_ENABLE=true`，问新闻/天气/股价等会实时搜再答；纯闲聊不每句搜。

中转站 502/503 时会重试中转，**不再**自动切本地 Ollama（避免小模型答非所问）。若要本地推理，需手动设 `LLM_PROVIDER=ollama`。

纯表情/贴纸曾被误当成「你好」（空正文兜底写错）。现已改为识别 face/mface/表情图，喂给模型「（发了个表情…）」。

引用别人消息再 @学姐时，会把【引用消息】+【我现在说的】一起交给模型。

群里 @ 和表情包不好发在同一条时：可先 `@学姐`，**60 秒内**再单独发表情包，也会当成找她；或直接「回复学姐的某条消息」再发表情包。

签到/积分/排行/任务回复已按「小卡片」结构排版（标题 → 短评 → 数据 → 称号/寄语/任务）。
