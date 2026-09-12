# NapCat 对接说明

## 目标

让 NapCat 以 **OneBot v11 反向 WebSocket** 客户端身份，连接到本仓库的 NoneBot2：

```text
ws://127.0.0.1:27315/onebot/v11/ws
```

## 步骤

1. 从 [NapCatQQ Releases](https://github.com/NapNeko/NapCatQQ/releases) 安装并登录机器人 QQ。
2. **先启动**本项目：`python bot.py`，确认日志里端口为 `27315`。
3. 打开 NapCat 网络配置 → 新建 → **WebSocket 客户端**（反向 WS）。
4. 填写：
   - URL：`ws://127.0.0.1:27315/onebot/v11/ws`
   - Token：与项目 `.env` 中 `ONEBOT_ACCESS_TOKEN` **完全一致**（若 Bot 侧为空，NapCat 也留空）
   - 消息格式：推荐 `Array`
5. 保存并启用连接。NoneBot 日志应出现 OneBot 连接成功记录。
6. 将机器人拉进目标群，发送 `@机器人 什么是过拟合` 验证。

## 启动顺序

必须：**NoneBot 先监听 → NapCat 再连入**。若反过来，NapCat 会连不上，通常会自动重试，也可手动重连。

## 常见问题

**已启动但收不到消息**

- 确认 URL 端口与 `PORT` 一致
- 确认 token 两端一致（403 多半是 token）
- 确认机器人在群内且有发言权限
- 若配置了 `DL_SENPAI_ALLOWED_GROUPS`，确认群号在白名单内

**能连上但不回复**

- 检查 `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL`
- 看 NoneBot 控制台是否有 `dl_senpai LLM error`
- `@` 时若未配置 Key，会收到学姐提示配环境变量

**改端口**

同时改 `.env` 的 `PORT` 与 NapCat URL 中的端口。
