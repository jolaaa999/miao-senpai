# B站 / 小黑盒只读层

给学姐「自己上网看东西」的第一个可用版本：刷 B 站、搜小黑盒帖子、读帖子正文。
**只读**：不登录、不点赞、不发帖、不评论。

## 为什么是这两家、这个顺序

社区里「大肥鱼插件生态」被提得最多的是三样：刷 B 站、发 QQ 空间说说、来小黑盒
（看帖/发帖/创号）。其中只有第一类不需要登录态，所以先做它——不用等主人扫码，
也不会以她的名义在任何地方留痕。

发说说、黑盒发帖属于写入层（文档末尾 T2），要先有持久登录态，且必须默认关。

## 结构

| 文件 | 职责 |
|---|---|
| `src/plugins/dl_senpai/bili.py` | B 站：热门、搜视频、看单个视频（标题/简介/播放/热评） |
| `src/plugins/dl_senpai/heybox.py` | 小黑盒：站内搜索、读帖（读帖交给 link_reader 的浏览器兜底） |
| `src/plugins/dl_senpai/quota.py` | 按天配额账本（两家共用，跨天自动清零） |
| `src/plugins/dl_senpai/agent/tools/social_tools.py` | 两个工具：marker `BILI` 和 `HEYBOX` |

## B 站为什么不走浏览器

实测四个接口匿名就能拿（`search/type`、`view`、`reply`、`popular` 都是 code=0），
只有 `ranking/v2` 会被风控挡（`code=-352`），所以「热门」改用 `web-interface/popular`。

一个重要细节：**直接打 API 拿不到 `buvid3`，B 站会对无 cookie 的请求回 -352**。
所以每次调用前先访问一次 `www.bilibili.com` 收 cookie（缓 6 小时），
遇到 -352 就丢掉旧 cookie 重来一次。少开一次 chromium，比浏览器方案快 5~10 秒。

## 小黑盒为什么要走浏览器

站内搜索接口 `/bbs/app/api/general/search/v1` 要 `hkey` 签名，服务端会校验：
只给 `_time`/`nonce` 一律回「非法请求」。社区里有人把签名逆出来了
（AES MixColumns 变体，靠故意发错签名读 403 回显里的中间值对拍），但那是硬维护活。

这里换一条稳的路：**Playwright 打开官方搜索页，页签自己带着合法签名去请求，
我们只在旁边用 `page.expect_response` 接住同一个响应**。签名留给浏览器算。

### 帖子 URL 的坑

小黑盒帖子有两种 URL：

- `https://www.xiaoheihe.cn/app/bbs/link/<数字id>` —— 数字 id 的页面渲染出来**只有一个页脚**
- `https://api.xiaoheihe.cn/v3/bbs/app/api/web/share?...&link_id=<hash>` —— 带 hash 的分享链接才能读到正文

所以 `HEYBOX` 的搜索结果里带的是 hash，读正文必须用 hash。

### 人机验证

连续抓取之后，内容接口会返回 `{"status": "show_captcha"}`，页面只剩页头页脚。
两道防线：

1. **只剩页头页脚不算读到**：`link_reader._looks_like_chrome` 拦掉（出现两条以上
   站点标识且总长度 < 800 字），并且这种结果**不写缓存**——否则会一直假成功下去。
2. **读失败进 10 分钟冷却**：`heybox._read_cooldown_until`，期间直接告诉她
   「刚才要求人机验证了，先歇十分钟」，比反复失败更诚实，也不会把封禁拖长。

## 配额

`quota.py` 按天计数，存 `data/dl_senpai/quota.json`：

- B 站默认 60 次/天、小黑盒 40 次/天（`DL_SENPAI_*_DAILY_LIMIT`，0 = 不限）
- **先扣再用**：先 +1 再看有没有超，避免并发请求同时「看到还没满」
- 接口报错/超时会 `refund_quota` 退回，不让一次超时白扣一格

为什么需要它：群里任何人都能指挥她「再去搜十个」，没有日上限就是白嫖接口 + 烧时间预算。

## 提示词里必须有的三句

1. 「你拿到的是**文字**：标题、简介、热评。你没看画面也没听声音，不要描述视频里
   出现了什么、说了什么」——否则她会把标题脑补成观后感。
2. 「搜索结果是标题和摘要，不代表你看过内容」。
3. 「读不到就说读不到，不要凭标题编」。

## 人机验证：登录态能解，匿名罐不够（2026-10-25 实测）

小黑盒对**陌生访客**的判定很直接：`/bbs/app/link/tree` 回
`{"status":"show_captcha"}`，页面只渲染页头页脚（268 字），正文一个字都拿不到。

实测三轮结论：

| 状态 | 读帖结果 |
|---|---|
| 全新匿名 context（每次都是陌生人） | ❌ 268 字，`empty_text` |
| 带上跑过一次攒下的匿名 cookie 罐（smidV2 / x_xhh_tokenid） | ❌ 仍然 268 字 |
| 带上**登录过小号**的 cookie 罐（29 个 cookie，含 heybox_id/pkey/fsa…） | ✅ 5388 字，正文完整 |

所以「人机验证能不能过」的答案是：**能，但要有个真身份在罐子里**。
匿名罐子只解决"看起来不是新设备"，解决不了"看起来是爬虫"。

做法上不是破解，是分工：

    python scriptslogin-web.py heybox      # 有头窗口里扫码登录小号（推荐）
    python scriptswarm-cookies.py heybox   # 只想混个罐子/过滑块时用（不需要账号）

登录态也会自动用在**只读**路径上：`link_reader` 和 `heybox_search` 一旦发现
`web_login/heybox.json` 就带上，跑完再把 cookie 回写（`persist_path`）——
罐子越用越像老访客。

**不做**的三件事：打码平台、逆向 hkey 签名、换 IP/代理绕过。理由分别是违规、
一改版就废、不属于"用平台"而属于"绕平台"。

## B站：留言与评论（2026-10-26）

B 站跟小黑盒的差别挺大：**它有公开的消息接口**，不用开浏览器翻页面。

| 事 | 接口 | 要什么 |
|---|---|---|
| 有人回我 | `GET /x/msgfeed/reply?pn=1&ps=20` | SESSDATA |
| 发评论 | `POST /x/v2/reply/add` | SESSDATA + bili_jct（csrf） |
| 我是谁 | `GET /x/web-interface/nav` | SESSDATA |

登录态在 `data/dl_senpai/web_login/bili.json`，由 `python scripts\login-web.py bili` 写入
（脚本会拒收主号）。**没有登录态就别开开关**，否则工具每次调用都只会报「还没登录」。

### 回复的 id 怎么来

「回复我的」一条消息里有三个 id，回的时候要用两个：

    reply.oid       -> 视频 aid（发评论时的 oid）
    reply.id        -> 对方那条评论 id  -> parent
    reply.root      -> 楼层根评论 id     -> root（缺失时退回 parent）

`_parse_msgfeed` 里把这些兜底都写死了，改接口时只改那一处。

### 实测的账号门槛（2026-10-26）

| 号的状态 | 发评论结果 |
|---|---|
| Lv0 未转正 | `code=-107 账号未转正` |
| Lv1 已转正 | `code=12074 努力升级到lv2再来评论吧` |

也就是说 **B 站评论的硬门槛是 Lv2**，不是「登录了就行」。
读留言（`x/msgfeed/*`）不受这个限制，Lv0 就能读——所以读和写要分开开。

### 养号到 Lv2（bili_grow.py）

Lv1→Lv2 要 200 经验，每日任务实测回报（`GET /x/member/web/exp/reward`）：

| 任务 | 经验 | 说明 |
|---|---|---|
| login | +5 | 访问首页就发 |
| watch | +5 | 点击上报 + `web/heartbeat` 心跳 |
| share | +5 | `x/web-interface/share/add` |
| coins | +1/枚（上限 10） | 要花真硬币，默认不投 |

所以**一天最多 +15**（不投币），一天一次、随机分钟跑：`DL_SENPAI_BILI_GROW_ENABLE=true`。
注意 `click/web/h5` 实测返回 `-400`，但心跳 `code=0`，经验照发——别被那个 -400 骗了。

### 为什么一次最多回两条

B 站对**账号行为**的风控比小黑盒硬：新号、低等级、短时间连发都容易 `-412`。
所以 `bili_inbox_max_per_tick=2`，两条之间还有 `bili_comment_min_gap_sec`（默认 900 秒）
和文案去重兜着。
## 看留言 / 回留言（2026-10-26）

**网页版没有消息中心**：`/app/message`、`/app/user/message`、`/app/notification` 等六七个
路由实测全部退回首页信息流，站点不提供 web 端通知列表。所以留言走另一条路：

    她评过的帖（台账里有 link_id）
      -> 开一次浏览器，读评论区（截 /bbs/app/link/tree 的响应）
      -> 找到"我那条评论所在的楼层"
      -> 挂在这条下面的回复 = 别人跟我说的话

实测结构（`_parse_tree_body` 就是照这个写的）：

    result.comments        = 楼层数组，每页 20 层，total_page=9
    floor["comment"]       = [根评论, 预览子回复1, 预览子回复2]
    根评论字段             = commentid / text / user.username / child_num / has_more / floor_num

`child_num > 预览条数` 时说明还有回复没给全，这种情况下回复里会带
`may_have_more: true`，让她知道"我可能没看全"，而不是假装看全了。

回复本身还是点 UI（和发评论同一套：点那条评论的「回复」→ ProseMirror 打字 → 发送），
`<<<HEYPOST reply 帖子hash|对方那半句|内容>>>`。她也能自己查：`<<<HEYBOX inbox>>>`。

### 评论条的真实 DOM（2026-10-26 抓到）

    评论条的最外层：div.link-comment__comment-item
      ├─ div.link-comment__comment-item-header   -> "九儿的小肥羊\n0\n4小时前\n·湖北"
      └─ div.comment-item__content-container
           └─ div.comment-item__content          -> 正文

「回复」按钮**默认不存在于 DOM**，hover 到评论条上才出现（所以按 ID 定位、
按纯文本找都会失败——踩过：报「没找到回复按钮」）。规矩是：
先用 .link-comment__comment-item 按文案过滤 → hover → 再找「回复」。

### 一条硬约束：访问密度

2026-10-26 实测：**登录态正常**（`/account/restore_login` 一直 ok，昵称九儿的小肥羊），
但短时间连开十几次浏览器上下文之后，`link/tree` 开始回
`{"msg":"","result":{},"status":"show_captcha"}`，正文页也只剩 270 字。
这不是登录过期，是行为风控。实测耐受度大约是**十几分钟一次浏览器会话**，五分钟内连开两次必被挡；卡住时正文页只剩 270 字，link/tree 直接回 show_captcha。所以加了 `_session_gate`：
两次开浏览器至少隔 `DL_SENPAI_HEYBOX_SESSION_GAP_SEC`（默认 240 秒），
且一次会话里尽量把多篇帖子读完（`heybox_thread_many`），不要一篇一个上下文。

## 她自己刷帖子（自主时间线）

先看别人怎么做的（2026-10 调研，参考实现是开源的 X Bot）：

    APScheduler 定时
      -> Selenium 抓首页前 N 条
      -> 按"人格兴趣标签"给每条打分，命中进灵感队列
      -> 从队列生成候选内容
      -> 重复检测（embedding 相似度 0.85）
      -> 发布 + 状态落盘 + 数据分析面板

Reddit 侧的公开经验更直接：**bot 被封基本不是因为"是 bot"，而是因为刷屏、不看版规、
回的都是通用废话**。做法是降频、先听再说（LURK：Listen / Understand / Reply / Keep logs）、
每条都留日志、只在真能补充价值时开口。

学姐这边落地成四步（`src/plugins/dl_senpai/heybox_browse.py`）：

    定时醒来（随机 1~3 小时，夜间静默）
      -> 刷首页信息流（浏览器截 /bbs/app/feeds 的响应，不逆向签名）
      -> LLM 自己挑一篇真想看的（挑不中就结束，不硬评）
      -> 读正文
      -> 掷骰子（默认 0.35）-> LLM 判断"有没有话说" -> 说就评一句

关键差别：**挑哪篇、评不评、评什么，都是她自己判断**，不是关键词规则。

### 为什么不按天卡条数

原来我设了"每天最多 3 条"。这其实是错的设计：卡条数会让她攒着补发，
反而更像机器人。现在 `DL_SENPAI_HEYBOX_COMMENT_DAILY_LIMIT=0`（不限），
换成四条软约束（`write_guard.py`）：

| 约束 | 默认 | 为什么 |
|---|---|---|
| 两条评论最小间隔 | 600 秒 | 连续评论是最像脚本的信号 |
| 同一帖只评一次 | 硬性 | 重复出现在同一帖＝骚扰+刷屏 |
| 文案相似度去重 | Jaccard ≥ 0.72 就拒 | 复读同样的话最容易被判 spam |
| 被挡就退避 | 20 分钟 | 硬冲只会把恢复时间推远 |

台账落 `data/dl_senpai/heybox_write_state.json`（谁在哪篇说了什么），
刷帖流水落 `data/dl_senpai/heybox_browse_log.jsonl`。主人随时能翻。

### 必须说清的风险

自动评论在任何社区都有被判垃圾信息的可能，账号是主人的小号。
上面四条约束只做到"低频像人"，做不到"绝对安全"。
想彻底稳妥，就把 `DL_SENPAI_HEYBOX_BROWSE_COMMENT_PROBABILITY` 压到 0.1 以下，
或者干脆关掉 `BROWSE_ENABLE`，只保留她"你说一句她才评"。

## 评论链路（2026-10-25 实测抓到的三步）

写接口同样要 hkey 签名，所以走浏览器点 UI。三步都留了退路：

1. 点底部固定条 `.hb-bbs-link__reply-target`（文案「评论 (已有 N 条评论)」）
2. 输入框是 ProseMirror 的 `div.ProseMirror.hb-editor` —— **没有 textarea**，
   而且不能用 `fill()`：不触发输入事件，ProseMirror 收不到字。必须先 click
   再用键盘事件打字
3. 发送按钮 `button.link-reply__menu-btn.hb-color__btn--confirm`（文案「发送」）

`heybox_comment(..., dry_run=True)` 做到"字打进去了"就停、绝不点发送，
用来验证链路而不留公开痕迹（已实测通过）。

## T2（已实现，默认关）：写入层

发 QQ 空间说说、给黑盒帖子评论。**默认全关**，且工具名在
`permissions.HIGH_RISK_TOOLS` 里——非管理员连名字都看不到。

| 文件 | 职责 |
|---|---|
| `src/plugins/dl_senpai/web_login.py` | 持久登录态：storage_state 落盘、解析登录账号、主号护栏 |
| `src/plugins/dl_senpai/qzone.py` | 说说：内容清洗、草稿存储、emotion_cgi_publish_v6 发布 |
| `src/plugins/dl_senpai/agent/tools/write_tools.py` | 两个工具：marker `QZONE` 和 `HEYPOST` |
| `scripts/login-web.py` | 有头浏览器手动登录一次，把 cookie 存下来 |

### 怎么启用

    1. python scriptslogin-web.py qzone     # 扫码/密码登录**小号**，回车保存
    2. .env 里加 DL_SENPAI_QZONE_ENABLE=true
    3. 重启 bot
    4. 私聊里说「写条说说：今天风好大」→ 她存草稿；再说「发」→ 真发

黑盒评论同理：`python scripts\login-web.py heybox` + `DL_SENPAI_HEYBOX_WRITE_ENABLE=true`。

### 四道门（串联，少一层都不行）

1. **开关**：`qzone_enable` / `heybox_write_enable` 默认 false，false 时工具根本不注册。
2. **可见性**：工具名在 `HIGH_RISK_TOOLS`，非管理员既看不到也执行不了。
3. **触发者校验**：执行时再验一次 `cfg.is_owner(context.user_id)`——不是主人本人直接拒。
4. **私聊 + 配额**：`qzone_auto_post=false` 时，publish 必须来自**私聊**
   （群里谁喊一句「发」都不算）；说说每天 1 条、评论每天 3 条。

### 只接小号

登录完成后会从 cookie 里解析 `uin`，若等于 `DL_SENPAI_OWNER_IDS` 里的号，
直接拒收并删掉这份 state。**用主号发东西，出事不可逆。**

### 说说的发布链路

    p_skey --hash33--> g_tk
    空间首页 HTML --正则--> qzonetoken
    POST emotion_cgi_publish_v6 (content + 固定字段)

不走 Playwright 点 UI 的原因：说说输入框是富文本编辑器，选择器和弹层一改版就废，
失败时也说不清是「没找到按钮」还是「被风控」。接口这条路失败原因明确得多。

**诚实标注：这条链路还没实测**（要等主人登录一次才能验）。第一次真发之前，
`qzone.publish(dry_run=True)` 只验到「cookie/g_tk/qzonetoken 都齐了」就停，不提交。

### 黑盒评论为什么只能点 UI

写接口和搜索接口一样要 `hkey` 签名（服务端校验），所以带着登录态开帖子页，
找输入框、填字、点「发送」。失败原因分得清：没登录 / 找不到输入框（改版）/
点完没生效（被拦）。**发帖**暂不做——小黑盒发帖要创作中心权限，未验证。
