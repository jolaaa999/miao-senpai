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

### 自主刷B站

`bili_browse.py`：按作息去刷 B 站首页，挑一个视频看标题/简介/热评**和字幕**，
点赞，偶尔收藏，再决定要不要写进日记 / 记忆本。评论等 Lv2。

链路：`bili_hot(12)` → 模型挑一个（可 pick 0 = 都不想看）→ `bili_video` 拿详情+热评
→ `bili_transcript` 读字幕 → （评论，Lv2 后）→ 模型一次问三件事
（日记 / 记忆 / 收藏）→ 点赞、收藏 → 骰子只管"留不留文字" → 写日记 / 记忆本

节奏跟小黑盒共用同一张作息权重表（人只有一个作息）：基础 **20~60 分钟**一次，
每天最多 **40** 次，3~8 点静默。实测按权重算下来一天约 **38 次**（会先撞到 40 的上限）。

为什么从 90~240 分钟改成 20~60：主人要的是「像真人一直刷」——真人刷 B 站十几分钟
一个视频很正常，原来一天只跑 6~8 次，看起来像坏了。代价是首页推荐会被刷得越来越窄
（看重了什么就更多推什么），所以 `_PICK_INSTRUCTION` 里保留了 pick 0 = 都不想看。

为什么产出是日记而不是消息：日记本来就是「她自己的一天」，
「今天在B站刷到一个讲XX的」天然属于这个范畴，而且不需要动平台接口。

### 刷到的东西怎么变成记忆

三处落点，各管一件事，**不要混**：

| 落点 | 是什么 | 存哪 | 生命周期 |
|---|---|---|---|
| 日记 | 她自己的一天 | `data/dl_senpai/diary/YYYY-MM-DD.md` | 会分层、会过期 |
| **B站记忆本** | 刷到且真值得记住的知识点 | `data/dl_senpai/bili_memory.json` | 长期，上限 200 条 |
| 人物记忆 | 关于主人的事 | person_memory | 硬上限 |

为什么第三条不能放前两条：日记会过期，人物记忆是「关于你」——一条讲买车的视频
跟她对主人的了解没关系，塞进去会把人物画像冲淡。

判定发生在同一次模型调用里（`bili_browse._NOTE_INSTRUCTION`）：

    {"keep": true/false, "note": "日记那句",
     "remember": true/false, "topic": "买车"}

remember=false 就只是「看过了」。去重两道：同 bvid 不再记；跟已有条目
bigram 相似度 ≥0.8 不记（同一个知识点不该占两个位置）。

### 点赞 / 收藏

刷到看完的视频，她会点赞；觉得值得回看才收藏。

    bili_like(bvid)              # POST x/web-interface/archive/like  like=1 / 2(取消)
    bili_favorite(bvid)          # POST x/v3/fav/resource/deal  add_media_ids / del_media_ids
    default_fav_folder()         # GET  x/v3/fav/folder/created/list-all 找「默认收藏夹」

分工的取舍：

- **点赞 = 配置决定**（`BILI_LIKE_MODE=always`），不问模型。点赞几乎没有表达成本，
  主人要的就是「刷到的会点赞」，多问一次模型只是多一次跑偏的机会。
- **收藏 = 模型判断**。收藏等于承诺「我以后还会回来看」，每篇都收等于把收藏夹变垃圾
  桶——那比不收藏更糟。所以提示词里写死：教程/方法/以后要照做的才收，爽一下就过的别收。

节制靠 `bili_actions.py` 这个日账本：同一条视频不会赞两次（重复赞是最明显的脚本特征），
当日上限 `BILI_LIKE_DAILY_MAX=12` / `BILI_FAV_DAILY_MAX=6`，跨天自动清零。
失败（-412 风控、-101 掉登录态）只记台账不算数，不然一次失败就把当天额度吃掉。

实测（2026-10-27）：点赞、取消赞、收藏、取消收藏四个动作都成功，
默认收藏夹 id 4364721869。验证完立刻还原了，没在号上留痕。

### 字幕会串台（2026-10-27 修）

同一个 BV、同一个 cid、同一个字幕 id（`ai-zh` 21369249），**连着问三次拿到三段
完全不同的内容**：一次讲珍妮·古道尔，一次是视频本身，一次在讲「我的兴趣爱好」。
路径 hash 每次都变，是 B 站那边的 AI 字幕对象在串。

她那天的日记因此写成了「字幕讲的是锅包肉，跟标题对不上，等于白看」——她能察觉
对不上，但不知道是自己拿到了别人的字幕。

现在 `bili_transcript` 的做法：**连取两次，首句一致才认**，不一致就再问一次
（换一份字幕对象），三次都不一致就当没字幕返回空串。宁可不给，也不给错的。
代价是每个视频多一次请求（十几 KB），换来日记不再胡说。

### 她真的能看画面了（2026-10-27 开）

`video_vision.py`：`playurl` 拿直链 → ffmpeg 按时间点抽帧 → 视觉模型一次看完，
返回 `{scene, text, style, summary}`，塞进写日记那一步的 payload（标成「我抽帧看到的」，
跟字幕分开，她得知道哪句是自己看见的）。

参数：`VIDEO_WATCH_FRAMES=8`、`VIDEO_FRAME_WIDTH=960`、`VIDEO_VISION_MAX_TOKENS=2000`。

实测（2026-10-27 21:06）：

```
「单三老太全局m700和aw绝航清图」
  画面：第一人称射击，玩家持枪在室内外移动、翻越矮墙，切到装备与背包界面，最后开镜瞄准。
  字：WORKING AREA / DIRECTOR OFFICE / 显示装备 / 940,500 / 09:56

「不减136斤不结婚，减掉还赢25万美金」
  画面：西装男在加长林肯旁开场，随后两人参加 MrBeast 的长期挑战：爬网、被直升机吊起、
        拉卡车、室内跑步，最后搬进新家相拥。
  字：because when they finally get married / DAY48 / 野兽先生
```

这些细节标题和字幕里都没有。

**边界（必须记住）**

1. 抽 8 帧不是看完视频。连续动作、剪辑节奏、一闪而过的字看不到。
2. 没有 OCR 引擎（tesseract/rapidocr 都没装），画面上的字是**视觉模型读的**，小字会漏。
3. 单轮 25~50 秒（抽帧 1~2 秒 + 视觉推理 25~48 秒），B站 40 轮/天大约多花 20~30 分钟算力。
4. **这个模型带思考链**：`max_tokens` 给小了正文会是空的（实测 300 全被思考吃掉）。
   所以默认 2000，且被截断时自动翻倍重试一次；再失败就退化成「整段当描述」，
   不白丢 8 帧。
5. 抽帧失败（ffmpeg 没装、付费番剧没直链、抖音直链过期）只记 `vision_error`，
   她照常刷、照常写日记——只是这轮少一段画面。

### 播放心跳：让「看过」留下痕迹（2026-10-27）

主人的疑问：B 站历史里看不到她今天刷的视频。原因不是 bug —— **历史只认播放器上报**。
她那条链路是 `view` / `player/v2` / 字幕 JSON，平台根本不知道她「看过」。

现在每轮刷完补两条上报（`BILI_WATCH_REPORT_ENABLE`，默认关）：

```
POST /x/click-interface/click/web/h5      起播
POST /x/click-interface/web/heartbeat     进度（真正写历史的这个）
```

播多久：`BILI_WATCH_REPORT_SECONDS=0` 表示按视频时长随机取（60 秒起，最多 5 分钟），
并**钳在视频真实时长之内**——报 99999 秒一眼是假的。两个上报之间隔 1.2 秒，
跟网页播放器一个节奏。

实测（20:48）：发完 3 秒查历史，第一条就是 `BV1J7nX6PEu6`，`progress: 180`，
和人在网页播放器留下的痕迹一样。函数自身还会回头查一眼历史（`watch_in_history`），
因为「上报成功」不等于「平台真记」。

顺带的好处：观看时长算养号经验。她卡在 Lv1 175/200 差 25 点，就是卡在没观看记录。

### 每刷一个就刷新一次首页（2026-10-27 改）

主人提的做法对：刷完一个视频就该刷新页面，别让「热榜」当固定菜单。

B 站这条链路没有浏览器页面（走的是接口），所以「刷新页面」对应的是**重新取一次流**。
原来 `bili_hot` 固定打 `popular?pn=1`，热榜几小时不动，她就把同一个 BV 点了 7 次。

现在 `bili_feed()`：

1. 优先首页推荐流 `/x/web-interface/wbi/index/top/feed/rcmd`，`fresh_idx` 随机（等于
   「往下滑了几屏」）。实测连叫两次 **重合 0 条**——这就是「刷新页面」该有的效果。
2. 推荐流挂了退回热门**随机页**（`pn` 1~5）。实测页间重合也是 0；固定 `pn=1` 正是
   原来那个 bug。
3. 返回 `(视频列表, 来源)`，来源写进台账（`source: rcmd|popular`），出问题一眼看得出走哪条路。

`bili_hot` 保留旧名字，内部走 `bili_feed`，所以聊天里让她「刷刷B站」也是每次新的一批。

### 同一个视频别反复刷（2026-10-27 修）

热榜不动，她就一直点同一行：`BV1btH36uEru` 一天被挑中 7 次，日记里堆了四条
意思一样的话。现在挑之前先把最近 `BILI_BROWSE_DEDUPE_HOURS`（默认 12 小时）
刷过的剔掉；整榜都刷过就跳过这一轮（`all_seen`），不重播旧视频。

### 她能不能「看」视频：读字幕

结论先写：**看不了画面**（模型是纯文本），**能读字幕**。

    bili_transcript(bvid, cfg=cfg, max_chars=2000)  # bili.py
    1. x/web-interface/view  取 cid
    2. x/player/v2           取 subtitle.subtitles 列表
    3. aisubtitle.hdslb.com  拉逐句 JSON，按 15 秒合并成带时间点的文字稿

三个必须知道的坑：

1. **匿名的 player/v2 里 subtitles 恒为空数组**，必须带 SESSDATA。所以这个接口传
   登录 cookie（`_get(..., cookies=login)`），而热门/搜索那些仍然是匿名。
2. **AI 字幕有幻觉**。实测把口播听成「投降 包村 拥 威信 扫」这种词。所以优先
   人工字幕（zh-Hans/zh-CN），其次 ai-zh，并且提示词里明写「别逐字引用」。
3. **不是每条都有字幕**。冷门、纯音乐、刚发布的常常没有——返回空串，不是报错。
   实测「吴站长 Uptown Funk」那条就是 0 字，她只能凭简介写日记。

按 15 秒合并的理由：B 站字幕一句常常四五字，一句一行会变成几百行，token 全花在
时间戳和换行上。合并后读起来像正常说话段落，又保留「大概第几分钟」的定位感。

拿到字幕之后，日记/记忆/评论都基于真实内容，而不是复述标题。开关
`BILI_BROWSE_TRANSCRIPT_ENABLE`（默认开）、字数上限 `BILI_TRANSCRIPT_MAX_CHARS=2000`。

### 刷到的东西怎么「影响」她

只落盘是没用的：文件在那儿，下一轮聊天她还是什么都不知道。所以加了
`browse_influence.py`——把最近刷到的压成一小段，挂在 `diary_brief` 后面，
**每一轮进 prompt**。

    数据源：B站记忆本（她真记住的） + 小黑盒台账（评过的优先，只是看过的次之）
    窗口：最近 BROWSE_INFLUENCE_HOURS=48 小时
    条数：最多 BROWSE_INFLUENCE_MAX_ITEMS=4 条

为什么挂在 diary_brief 上：那是唯一一个「每轮都进 prompt、又不与并发改动冲突」
的通道（builder.py / legacy_bridge.py 有人同时在改）。日记关掉时这段照样注入——
影响渠道不该被日记开关连带关死。

提示词里明写「别把这段原文念出来」：模型拿到清单最爱逐条播报
（我昨天看了A，还看了B…），那不是受影响，那是念稿。

### 入口：怎么问她刷到过什么

+ **对话里**：她自己有 `<<<BILIMEMO>>>` / `<<<BILIMEMO 买车>>>`，问她就查得到
+ **命令行**：`python scripts\\bili-memory.py [关键词]`，加 `--diary` 只看日记
+ **直接翻文件**：记忆本 `bili_memory.json`，日记 `diary/2026-10-26.md`

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
### 怕密不怕多：请求要一个一个丢（2026-10-27）

主人的判断对：小黑盒不是怕「多」，是怕「密」。一次只丢一个请求基本没事，
短时间连着丢一串就回 `show_captcha`。

所以 `_session_gate` 从「一个固定间隔」拆成两档：

| 档位 | 谁走 | 间隔 |
|---|---|---|
| 重 | 发评论、翻评论区（一次会话读多篇） | `HEYBOX_SESSION_GAP_SEC`，默认 240 秒 |
| 轻 | 读正文、看信息流、看留言 | `HEYBOX_REQUEST_GAP_SEC`，默认 45 秒 |

为什么必须分档：只有一个 240 秒的闸门时，群里有人说「看看这个帖子」，
她要干等四分钟才动；但读又不能完全不登记——**以前读正文这条路一次都没过闸门**，
那是昨天连着读到 0 字的主要漏口。

同一会话内的连续动作也加了气口：展开回复的两次点击之间 0.7~1.6 秒，
换帖之间 4~9 秒，滚动 2.5~5 秒（原本就有）。目标只有一个：任何时刻只有一个请求在飞。

### 怎么不被当成脚本（2026-10-28）

诊断：小黑盒一天撞 4 次验证，但同一篇帖子 12:32 读 0 字、16:12 读 1993 字——
**不是封号，是这个访客可疑**。可疑点全在代码里：

| 破绽 | 表现 |
|---|---|
| 每轮新 launch + new_context | 一周 70 个「不同浏览器」用同一份 cookie |
| `navigator.webdriver = true` | 一行 JS 就能读到 |
| 插件列表为空 | 真 Chrome 有 5 个 PDF 插件 |
| 无 `window.chrome`，languages 只有 zh-CN | 无头环境特征 |
| WebGL 报 SwiftShader | 软件渲染，无头环境特征 |
| 直接 goto 详情页 | referer 为空，页面上零交互 |

`browser_stealth.py` 一次解决：

```
LAUNCH_ARGS        --disable-blink-features=AutomationControlled，忽略 --enable-automation
STEALTH_JS         覆写 webdriver / 补 plugins / languages / chrome / WebGL vendor / permissions
open_page()        持久 profile（data/dl_senpai/browser_profiles/<site>），cookie 与指纹都长在里面
human_touch()      鼠标移动 2~4 次 + 滚 2~5 屏 + 停 0.6~1.6 秒再抽正文
link_reader        先到站点首页停一下，再带 referer 进正文（站内跳转才像人）
heybox_avoid.json  撞过验证的帖子 6 小时内不再碰，换目标比硬顶划算
```

实测（08:28）：heybox feed 12 条 8.8 秒、读正文 6014 字 27.9 秒；抖音 feed 6 条 16.4 秒；
两个 profile 目录分别落盘 221 / 587 个文件——cookie 和指纹从此是同一个熟人。

**边界**：不可能清零。风控是持续对抗，这套只是把一眼假变成要看一会儿。
第一次建 profile 时会自动把旧的 `web_login/<site>.json` cookie 灌进去，不用重新扫码。

### 验证退避分两档（2026-10-28 改）

原来一撞验证就停 3 小时（`CAPTCHA_BACKOFF_SEC=10800`）。实测同样一篇帖子，
12:32 读是 0 字、16:12 再读就有 1993 字——验证是**闪一下**的，停 3 小时等于白躺。

10-27 一天撞 4 次 × 3 小时 = 12 小时在睡，用户看到的就是「好几个小时才一条评论」。

现在（`heybox.note_captcha`）：

```
第一次撞   → 25 分钟后再来看
连着第二次 → 才算真被盯上，歇 3 小时
成功读一次 → 连击清零，下次又从 25 分钟开始
```

同时把节奏提上来：间隔 `2400~5400s`（40~90 分钟，原来 1~2.7 小时），
日上限 `14 → 20`。评论量预计从 2~3 条/天到 6~8 条/天。

### 读正文要重试，撞验证要退避（2026-10-27）

那天 12:32~15:03 连续 4 次 tick 全是 `read_chars=0`，等于四个小时没刷成、也没评成。
16:12 手工再读同一篇，立刻拿到 1993 字——**风控是闪一下的，一次失败不代表被封**。

改了两处：

1. `browse_once` 读正文失败会**隔 2.5 秒重试一次**（那 4 次里至少 3 次能救回来）；
2. 两次都拿不到字 → 记 `blocked=true`，循环按 `HEYBOX_CAPTCHA_BACKOFF_SEC`（默认 3 小时）
   退避。不这么做的后果是：越是按 2 小时硬撞，验证顶得越死。

**代价要说清楚**：被风控期间，「每两小时必刷一篇」做不到——那是平台在拦，
不是代码偷懒。退避是唯一正确反应。

### 小黑盒为什么不做点赞/收藏（2026-10-27 定）

实测过三种页面形态（`api.xiaoheihe.cn/.../web/share`、`/app/bbs/link/{id}`、
`/bbs/app/link/{id}`）：**帖子级的点赞、收藏在网页端根本没有入口**，页面上只有
「关注」和「评论」，全文搜不到「赞」「收藏」两个字。DOM 里能点到的赞只有
`button.like-box`——那是给**别人的评论**点赞，不是给帖子。

调接口也不行：小黑盒写接口要 hkey 签名，服务端校验（评论当初就是因为这个改成点 UI 的）。
逆 hkey = 跟着前端版本反复修 + 更高的风控概率，为一个「赞」不划算。

**决定（主人选的 A）**：小黑盒只做「刷 + 评论」，点赞/收藏集中在 B 站和抖音。
别再往这条路加代码，也别指望以后能找到入口。

### 每两小时必须刷一篇（硬上限）

`_next_gap_seconds` 原来把间隔钳在 5 小时，低谷时段（权重 0.5）能拉出 4 小时空档。
现在改成硬上限 `HEYBOX_BROWSE_MAX_GAP_SEC=7200`：基础间隔再大也不许超过 2 小时。
测试盯的就是这件事（200 次采样全部 ≤ 7200）。

注意：3~8 点静默仍然存在，那段时间会有 5 小时空档。要真的 24 小时不断，
得把 `own_life_quiet_*` 一起关掉——那是她整个人的作息，不只是刷帖。

### 刷了就评论（2026-10-27 改）

原来有三道闸：骰子（0.25）→ 模型判断「想不想说」→ 文案护栏。实测下来
**七次刷帖只出一条评论**，看起来像坏了。主人要的是「刷了就说」，所以改成：

1. 骰子关掉：`HEYBOX_BROWSE_COMMENT_PROBABILITY=1.0`（`random() > 1.0` 永不成立，等于不掷）
2. 模型的 `comment:false` 不再一票否决：**只要它给出了文案就发**，只有文案空着才跳过
3. 提示词从「多半时候没什么好说的」改成「既然点开了就说一句」

留下的护栏只有三道：同帖只评一次、两条评论间隔 ≥600 秒、文案 bigram 相似度 ≥0.72 判重。

代价说清楚：tick 恢复后一天可能 5~10 条评论，小黑盒对高频评论会给人机验证。
撞上时会自动 `CAPTCHA_BACKOFF_SEC`（默认 3 小时）冷静，日志里能看到 backoff 那行。

### 台账在哪看

    python scripts\\heybox-log.py          # 最近 15 条，带每条的结局
    python scripts\\heybox-log.py --day    # 按天汇总：刷了几次、评了几条、骰子挡了几次

原始文件 `data/dl_senpai/heybox_browse_log.jsonl`，一行一次 tick。

### 刷帖频率怎么定（作息权重）

均匀随机间隔是最容易被识别的机器特征——真人是**扎堆刷**的。所以间隔改成
`基础间隔 ÷ 当前小时权重`，权重表在 `heybox_browse._HOUR_WEIGHT`：

| 时段 | 权重 | 效果（基础 60~160 分钟） |
|---|---|---|
| 03–08 | 0 | 不刷 |
| 00–03 | 0.5 | 120–320 分钟 |
| 08–12 / 14–18 | 1.1–1.3 | 45–145 分钟 |
| 12–14 | 1.9–2.0 | 30–85 分钟（午休） |
| 18–20 | 1.7–1.8 | 33–94 分钟（晚饭后） |
| 20–24 | 1.6–2.2 | 27–100 分钟（全天高峰） |

再叠三层刹车：每日上限 `BROWSE_DAILY_MAX=14`、12% 概率偶发连刷（2–8 分钟后再点开）、
撞人机验证自动冷静 `CAPTCHA_BACKOFF_SEC=10800`（3 小时）。

蒙特卡洛 60 天实测（`_next_gap_seconds` 真跑）：

    去掉日上限约 17 次/天，带上限落在 14 次/天（原方案 9~10 次/天）
    高峰 20–23 点每小时约 1.3 次，凌晨 1 点约 0.2 次

**评论概率同时下调到 0.25**：逛得更勤但说得更多，才是最容易露馅的组合。
现在每天评论量约 1.5~2 条，与调频前持平。

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

### 抖音（只读）

`douyin.py`。跟 B 站最大的不同：**没有能直接调的公开接口**。

抖音 web 每个 `/aweme/v1/web/*` 请求都带 `a_bogus` 签名，签名是混淆 JS，
跟着前端版本变。自己复刻等于每隔几周修一次，被封的成本还是账号。

所以做法是：真浏览器打开页面，**拦截它自己发出的请求的响应**——签名交给抖音
自己的页面算，我们只读回来的 JSON。跟小黑盒那套一个思路。

限制（都是真的，不要抱幻想）：

1. **必须登录**。匿名打开首页直接弹登录框，推荐流根本不加载。登录走
   `python scripts\login-web.py douyin`（有头窗口扫码）。
2. **慢**。一次读要开一次 chromium，10~20 秒；比 B 站接口慢一个数量级。
3. **结构会变**。抖音前端改版比 B 站勤，解析对不上时打开
   `DL_SENPAI_DOUYIN_DUMP_RAW=true` 把原始响应存到 `data/dl_senpai/douyin_raw/`，
   照着改，不要靠猜。
4. **拿不到字幕**。抖音没有公开字幕接口，所以「读内容」只有描述 + 热评。
   她写日记时会比 B 站那条浅，这是平台给的极限。

链路：`dy_feed()` 拦 `/aweme/v2/web/module/feed` → `dy_video(id)` 拦评论/详情接口
→ `render_feed` / `render_video`。

实测（2026-10-27）踩到的两个细节：

1. PC 端「精选」页走的是 **v2** `module/feed`，不是 v1 的 `tab/feed`——第一次写错，
   抓到的响应列表里根本没有 feed，白等 40 秒。
2. **一次只回 3 条**，要往下滚才继续加载。所以 `dy_feed` 是"边滚边收"，
   收到 5 份响应或超时才停。

### 自主刷抖音

`douyin_browse.py`：结构照抄 bili_browse，差别在"能读到什么"。

    dy_feed(12) → 模型挑一个（pick 0 = 都不想看）→ dy_video 拿描述+热评
    → 模型一次问三件事（日记 / 记忆）→ 写日记（tags=["抖音"]）/ 记忆本

- 间隔 30~90 分钟（基础值）/ 作息权重 / 3~8 点静默 / 每日上限 `DOUYIN_DAILY_LIMIT=12`
- 比 B 站稀，因为一次要开一次 chromium，10~20 秒
- 记忆走 `douyin_memory.json`，去重和上限直接复用 `bili_memory`（套了个
  `SimpleNamespace` 的壳，因为那套逻辑跟平台无关，抄一遍只会慢慢长歪）
- **不做点赞/收藏**：抖音的赞要么点 DOM（改版即崩，还可能误触），要么调带 a_bogus
  签名的写接口。读层先跑稳。

实测第一条产出（2026-10-27 10:00）：

    挑到：《三角洲行动s10机密小M7…改枪码：SCAR-H战斗步枪-烽火地带-6KRNCS401》
    日记/记忆：刷到个SCAR-H改枪码，S10赛季的，先存着回头试。

回流层（`browse_influence`）也接了抖音台账，所以刷到的东西一样进每轮提示词。
