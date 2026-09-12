# 浏览器逛网站（学姐 + Cursor MCP）

学姐可在 QQ 里打开网页、淘宝搜商品、Pixiv 搜壁纸，并把截图/图片发到群或私聊。

## 学姐侧（QQ Bot）

默认开启（`DL_SENPAI_BROWSER_ENABLE=true`）。模型在回复末尾写 `<<<BROWSE ...>>>` 后，bot 用 Playwright 执行：

| 指令示例 | 作用 |
|----------|------|
| `<<<BROWSE screenshot https://example.com>>>` | 打开网址截图 |
| `<<<BROWSE taobao 黑色卫衣 女>>>` | 淘宝搜索并截图 |
| `<<<BROWSE pixiv 明日方舟 壁纸>>>` | Pixiv 标签搜索 |
| `<<<BROWSE image 可爱猫咪壁纸>>>` | 通用图片搜索 |
| `<<<BROWSE https://example.com>>>` | 等同截图网址 |

也可直接说「学姐帮我去淘宝找黑色卫衣」「去 pixiv 找壁纸」——系统会提示模型补写 `<<<BROWSE>>>`。

### 安装浏览器依赖

```bash
python -m pip install -e ".[browser,search]"
playwright install chromium
```

### 环境变量

| 变量 | 说明 | 默认 |
|------|------|------|
| `DL_SENPAI_BROWSER_ENABLE` | 是否启用 | `true` |
| `DL_SENPAI_BROWSER_TIMEOUT` | 单次超时（秒） | `90` |
| `DL_SENPAI_BROWSER_COOLDOWN` | 冷却（秒） | `30` |
| `DL_SENPAI_BROWSER_MAX_IMAGES` | 单次最多发几张图 | `3` |
| `DL_SENPAI_BROWSER_HEADLESS` | 无头模式 | `true` |
| `DL_SENPAI_BROWSER_ALLOWED_DOMAINS` | 截图 URL 白名单；空=任意公网 https；`*`=全部 | 空 |
| `DL_SENPAI_BROWSER_ALLOW_ON_INTERRUPT` | 插嘴时是否允许 | `false` |

截图缓存目录：`data/dl_senpai/browser/`

## Cursor MCP

项目已含 `.cursor/mcp.json` 中的 `qqbot-browser` 与官方 `playwright` MCP。

```bash
python -m pip install -e ".[mcp,browser,search]"
playwright install chromium
```

### 工具一览

| 工具 | 作用 |
|------|------|
| `browser_screenshot` | 打开 URL 截图 |
| `browser_search_taobao` | 淘宝搜索截图 |
| `browser_search_pixiv` | Pixiv 搜索 |
| `browser_image_search` | 通用图片搜索 |

## 注意

- 淘宝 / Pixiv 可能有登录墙、验证码；失败时学姐会提示稍后再试
- 单次任务约 30s～90s，请耐心等待
- Pixiv 原图有版权，建议作预览/参考，勿商用
