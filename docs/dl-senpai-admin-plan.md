# 学姐后台管理 — 计划与实现提示词

## 一、背景与目标

为 QQBot `dl_senpai` 插件建设独立 Web 管理后台，用于：

- 查看/管理聊天记忆（按 session 的 JSON 滑动窗口）
- 查看/管理签到数据、表情包库、热榜缓存
- 总览功能开关与运行状态（读取 `.env` 配置，只读展示）

**约束**：前后端 API 路径使用非常规前缀，避免与常见 `/api`、`/admin` 冲突。

## 二、技术选型

| 层 | 技术 | 端口 |
|----|------|------|
| 前端 | Vue 3 + TypeScript + Element Plus + Vite | `41788` |
| 后端 | Go 1.22 + Gin + GORM | `28473` |
| 管理库 | SQLite (`data/dl_senpai/admin/console.db`) | — |
| 业务数据 | 直接读写 `data/dl_senpai/` JSON（与 Python bot 同源） | — |

## 三、API 设计（偏门前缀）

**Base URL**: `http://127.0.0.1:28473/x7k9-dl-senpai-console/v1`

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/auth/token` | 登录，返回 JWT |
| GET | `/overview-pulse` | 仪表盘：session 数、签到人数、表情数、功能状态 |
| GET | `/senpai-sessions` | 聊天 session 列表（群/私聊） |
| GET | `/senpai-sessions/:sid/turns` | 某 session 对话记录 |
| DELETE | `/senpai-sessions/:sid/turns` | 清空某 session 记忆 |
| GET | `/checkin-roster` | 签到群列表 |
| GET | `/checkin-roster/:gid/users` | 群内签到用户 |
| PATCH | `/checkin-roster/:gid/users/:uid` | 修改积分/称号等 |
| GET | `/sticker-vault` | 表情 session 索引 |
| GET | `/sticker-vault/:sid/items` | 某 session 表情列表 |
| DELETE | `/sticker-vault/:sid/items/:id` | 删除表情 |
| GET | `/feature-switches` | 功能开关只读快照（解析 .env） |
| GET | `/trends-snapshot` | 热榜缓存只读 |
| GET | `/log-streams` | 运行日志文件列表 |
| GET | `/log-streams/:id/lines?tail=300` | 读取日志尾部 N 行 |

**鉴权**: `Authorization: Bearer <jwt>`（除 `/auth/token` 外）

## 四、目录结构

```
admin/
  backend/          # Go 服务
    cmd/server/main.go
    internal/{config,models,middleware,services,handlers}
    go.mod
  frontend/         # Vue 管理端
    src/{views,components,api,router,styles}
    package.json
    vite.config.ts
scripts/
  start-senpai-console.ps1   # 一键启动前后端
```

## 五、前端页面

1. **登录** — 暗色玻璃拟态，粉珊瑚强调色（安洁莉娜气质）
2. **总览** — 统计卡片 + 功能状态矩阵 + 最近 session
3. **聊天记录** — 左侧 session 列表，右侧气泡对话流
4. **签到管理** — 群切换 + 用户表格（积分/连签/称号/任务）
5. **表情库** — 网格预览 + 关键词/使用次数
6. **功能开关** — 分组展示 DL_SENPAI_* 配置（只读）

## 六、数据集成策略

- **读**: Go 扫描 `data/dl_senpai/memory/*.json`、`checkin/`、`stickers/`、`trends/cache.json`
- **写**: 原子写 JSON（与 Python `threading.Lock` 并存时尽量短写；管理端写操作需确认）
- **配置**: 解析项目根 `.env` / `.env.dev`（不暴露 API Key 明文，脱敏显示）
- **SQLite**: 仅存管理员账号、登录审计（可选）

## 七、安全

- 默认仅监听 `127.0.0.1`
- 环境变量 `SENPAI_CONSOLE_ADMIN_USER` / `SENPAI_CONSOLE_ADMIN_PASS`
- JWT secret: `SENPAI_CONSOLE_TOKEN_SECRET`
- 生产勿暴露到公网

---

## 八、实现提示词（喂给 Agent 执行）

```
请为 QQBot 项目的 dl_senpai 插件实现 Web 管理后台，严格按以下规格：

【项目根】e:\PROJECT\QQBot
【后端】admin/backend — Go + Gin + GORM + SQLite
  - 监听 127.0.0.1:28473
  - API 前缀 /x7k9-dl-senpai-console/v1（全部路由挂在此下）
  - 读取 QQBOT_ROOT 环境变量（默认上级两级到项目根）
  - 业务数据直接读写 data/dl_senpai/ 下 JSON（memory、checkin、stickers、trends）
  - GORM SQLite: data/dl_senpai/admin/console.db（管理员表）
  - JWT 鉴权中间件
  - 实现计划文档第三节全部 API
  - .env 功能开关解析时 API Key 脱敏（前4后4）

【前端】admin/frontend — Vue3 + TS + Element Plus + Vite
  - dev 端口 41788，proxy 到后端 /x7k9-dl-senpai-console
  - 暗色主题 + 粉珊瑚(#ff6b9d)强调 + 玻璃卡片，有设计感，避免通用 AI 审美
  - 页面：登录、总览、聊天记录、签到、表情库、功能开关
  - axios 封装，token 存 localStorage key: senpai_console_token

【脚本】scripts/start-senpai-console.ps1 同时启动 go run 与 npm run dev

【不要】修改 bot.py 或 dl_senpai 插件逻辑（v1 独立服务读 JSON 即可）
【不要】提交 .env 或真实密钥
```
