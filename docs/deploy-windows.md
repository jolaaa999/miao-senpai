# 进群 + 长期挂机（Windows）

机器人要进 QQ 群并一直在线，需要 **两套程序同时跑**：

1. **本项目 NoneBot**（大脑：学姐回复）
2. **NapCat**（身体：登录 QQ、收发群消息）

电脑关机 / 休眠 / 断网时机器人都会掉线。

---

## 一、一次性准备

### 1. 填好 `.env`

至少配置：

- `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`
- 建议设置 `ONEBOT_ACCESS_TOKEN`（和 NapCat 里一致）

可选：`DL_SENPAI_ALLOWED_GROUPS=你的群号`（只服务指定群）

### 2. 安装并登录 NapCat

1. 下载：[NapCatQQ Releases](https://github.com/NapNeko/NapCatQQ/releases)
2. 用 **小号** 登录（不建议主号）
3. 按 [napcat-setup.md](napcat-setup.md) 配反向 WS：

```text
ws://127.0.0.1:27315/onebot/v11/ws
```

Token 与 `.env` 的 `ONEBOT_ACCESS_TOKEN` 一致。

### 3. 把机器人拉进群

- 用你的大号 / 群主号：邀请 **机器人 QQ** 进目标群
- 确认机器人有发言权限（未被禁言、群没开「仅管理员发言」等）
- 群里发：`@机器人 什么是过拟合`

---

## 二、日常启动顺序

1. 先开 NoneBot  
2. 再开（或等）NapCat 连上  

手动启动 Bot：

```powershell
cd E:\PROJECT\QQBot
powershell -ExecutionPolicy Bypass -File .\scripts\start-bot.ps1
```

或：

```powershell
cd E:\PROJECT\QQBot
python bot.py
```

日志默认写到 `data\logs\bot-日期.log`。

---

## 三、开机自动运行（推荐）

双击项目根目录 **`注册开机自启动.bat`**（或命令行）：

```powershell
cd E:\PROJECT\QQBot
powershell -ExecutionPolicy Bypass -File .\scripts\install-autostart.ps1 -StartNow
```

会注册两个计划任务（你**登录 Windows 后**自动跑）：

| 任务名 | 作用 |
|--------|------|
| `QQBot-DL-Senpai` | 立即启动 NoneBot |
| `QQBot-NapCat` | 约 45 秒后启动 NapCat（等 Bot 先监听） |

NapCat 路径写在 `scripts\napcat-path.txt`（当前默认 `E:\NapCatQQ_Desktop\NapCatQQ-Desktop.exe`）。装到别处就改这个文件。

| 操作 | 命令 / 文件 |
|------|-------------|
| 注册并立刻启动 | 双击 `注册开机自启动.bat` |
| 立刻启动 Bot | `Start-ScheduledTask -TaskName QQBot-DL-Senpai` |
| 立刻启动 NapCat | `Start-ScheduledTask -TaskName QQBot-NapCat` |
| 取消全部自启 | 双击 `取消开机自启动.bat` |

说明：是「用户登录后」自启，不是未登录的纯开机服务。

---

## 四、想 24 小时在线

本机挂机可以，但笔记本合盖休眠会断。更稳的做法：

- 一台不休眠的小主机 / 云服务器（Windows 或 Linux）上同时跑 NapCat + 本 Bot
- 电源选项：关闭「睡眠」、接通电源时不休眠

---

## 五、自检清单

- [ ] `python bot.py` 日志出现 `Uvicorn running on http://127.0.0.1:27315`
- [ ] NapCat 显示 OneBot 已连接
- [ ] 机器人已在群内且能说话
- [ ] `.env` 中转站 Key 有效
- [ ] 登录后 Bot 与 NapCat 都会自动起来
