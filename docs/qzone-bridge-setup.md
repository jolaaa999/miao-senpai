# QQ 空间桥接（onebot-qzone / A 方案）对接说明

学姐通过 HTTP 调用 [onebot-qzone](https://github.com/Gu-Heping/onebot-qzone) 刷好友动态：点赞、评论。  
名片赞仍走 NapCat `send_like`，与空间无关。

## 1. 一键启动（推荐）

仓库内已有 `onebot-qzone/` 时，只需：

```env
DL_SENPAI_QZONE_ENABLE=true
DL_SENPAI_QZONE_AUTOSTART=true
DL_SENPAI_QZONE_SYNC_COOKIE=true
DL_SENPAI_QZONE_BRIDGE_URL=http://127.0.0.1:5700
```

然后只跑：

```bash
python bot.py
```

Bot 会：

1. 自动拉起 `onebot-qzone`（`npm`/`tsx src/main.ts`）  
2. NapCat 连上后用 `get_cookies` 登录空间（免扫码）  
3. 退出 Bot 时结束桥接子进程  

日志关键字：`starting qzone bridge` / `qzone bridge ready` / `qzone login via NapCat ok`。  
桥接日志：`data/dl_senpai/qzone-bridge.log`。

首次需在 `onebot-qzone` 目录执行过一次 `npm install`。

## 1b. 手动安装桥接（仅目录缺失时）

```bash
cd E:\PROJECT\QQBot
git clone https://github.com/Gu-Heping/onebot-qzone.git
cd onebot-qzone
npm install
```

## 2. 学姐侧配置（本仓库 `.env`）

```env
DL_SENPAI_QZONE_ENABLE=true
DL_SENPAI_QZONE_BRIDGE_URL=http://127.0.0.1:5700
DL_SENPAI_QZONE_ACCESS_TOKEN=
DL_SENPAI_QZONE_SYNC_COOKIE=true
DL_SENPAI_QZONE_DAILY_HOUR=10
DL_SENPAI_QZONE_LIKE_ENABLE=true
DL_SENPAI_QZONE_COMMENT_ENABLE=true
DL_SENPAI_QZONE_LIKE_DAILY_LIMIT=30
DL_SENPAI_QZONE_COMMENT_DAILY_LIMIT=10
DL_SENPAI_QZONE_COMMENT_PROB=0.35
DL_SENPAI_QZONE_COMMENT_TEMPLATES=好看！,冲！,学姐路过点个赞~,不错哦
```

`QZONE_SYNC_COOKIE=true` 时，学姐会把 NapCat `get_cookies(domain=qzone.qq.com)` 推给桥接 `login_cookie`，一般不用再手拷 Cookie（若失败再改用扫码/手动 Cookie）。

## 3. 可选：NapCat 原生插件

见 upstream `napcat-plugin/`：`npm run build:plugin` 后装入 NapCat，把桥接代理进同一条 OneBot。  
学姐默认走独立 HTTP，不依赖该插件。

## 4. 验证

1. 桥接已启动，NapCat 已连上 Bot。  
2. 重启 `bot.py`，日志应有 `qzone=on`。  
3. 过 `QZONE_DAILY_HOUR` 后约 1 分钟内出现：

   - `qzone liked uin:tid`
   - `qzone commented uin:tid`

数据去重写在 `data/dl_senpai/social/qzone.json`。

## 5. 风控

空间批量赞评仍可能触发限制；请保留默认配额与间隔，必要时降低 `LIKE/COMMENT_DAILY_LIMIT` 与 `COMMENT_PROB`。
