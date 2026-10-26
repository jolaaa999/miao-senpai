# 从「QQ 小鲸鱼」借鉴到学姐：落地清单

来源：小黑盒 @异世回忆 的 19 篇帖子（重点 3 篇教程）+ 仓库
`pinganbbOvO/ai-companion-kit`（含 `packages/companion`、`own-life`、`phone-mcp`）+
`Derpyu520/qq-bridge`。

原则：**默认值一律取「什么都不做」**。这批能力让学姐更像人，但其中几项能发出
撤不回的消息、或者动到桌面，所以升级时不该自动生效。

## 一、已落地

| 能力 | 落点 | 开关 | 默认 |
|---|---|---|---|
| 随机唤醒间隔（45~110 分钟随机，不再像闹钟） | `own_life.py` `plan_next_due` | `OWN_LIFE_INTERVAL_MIN/MAX_SEC` | 开（随 own_life） |
| 想说话还要过骰子（0.35） | `own_life.py` tick | `OWN_LIFE_SEND_PROBABILITY` | 0.35 |
| 你们正聊时不打断（安静 15 分钟才考虑） | `own_life.py` `_last_activity_ts` | `OWN_LIFE_MIN_SILENCE_SEC` | 900 |
| 断线补课（重连拉历史、只总结不逐条回） | `backfill.py` + `__init__` on_bot_connect | `BACKFILL_ENABLE` | **关** |
| 语音转文字（本地 whisper） | `asr.py` + `gateway.from_onebot_event_with_asr` | `ASR_ENABLE` | **关** |
| 她自己的相册（她挑图 + 写理由） | `gallery.py` + `agent/tools/gallery_tool.py` | `GALLERY_ENABLE` | **关** |
| 印象保鲜（14 天没更新就提醒她重看这个人） | `context_extras.py` | `IMPRESSION_REFRESH_*` | 开 |
| 周报（每周一次「我们这周」） | `weekly.py` + `weekly_loop` | `WEEKLY_ENABLE` | **关** |
| 上一条没发出去要告诉她 | `reply_send._record_send_failure` → `context_extras` | 无（总是生效） | 开 |
| 开场白去重（真的重复时才提醒） | `context_extras.note_outgoing` | `GREETING_DEDUPE_ENABLE` | 开 |
| 电脑（GUI）操作 | `computer_control.py` + `agent/tools/computer_tools.py` | `COMPUTER_ENABLE` | **关** |
| 硬规则必须在人设最前面 | `persona_lint._lint_hard_rule_position` | — | 体检时跑 |
| 日记别写成汇报 | `diary.compose_diary` 硬规矩第 9 条 | — | 开 |
| 读链接（自动读 + `<<<READ>>>` 工具，SPA/PDF/长文摘要） | `link_reader.py`、`agent/tools/link_tools.py` | `LINK_READ_*` | **开** |
| 一句话健康检查 | `scripts/health.ps1` | — | 手动 |

## 二、电脑控制：五道门，缺一不可

```
COMPUTER_ENABLE=false            → 工具根本不注册（模型连名字都看不到）
MODE=view                        → 只有截图/列窗口/看前台能过
MODE=control                     → 才允许点、打、按键、滚动
ALLOW_APPS 白名单（留空=谁都不许碰） → 前台窗口必须命中
租约（lease.json）                → 默认必须持有，到期自动失效
DENY_KEYWORDS                    → 支付/密码类窗口连截图都拒
+ 限流（20 次/分钟、300ms 间隔）+ 全量审计日志
```

用法：

```powershell
# 1. 只看：.env 里设 COMPUTER_ENABLE=true，MODE=view
# 2. 要动手：
#    MODE=control
#    ALLOW_APPS=notepad.exe,WeChat.exe,LDPlayer.exe
#    python scripts\computer-lease.py allow 10   # 授权 10 分钟
# 3. 随时查看/收回：
python scripts\computer-lease.py status
python scripts\computer-lease.py revoke
```

**这不是操作系统级隔离**：她拿到的是当前登录用户的完整桌面权限。
接模拟器或备用机，别接装着支付和密码管理器的主力机。

## 三、能不能跑在 iPad / 安卓上

先说结论：**这台学姐本身跑不了**，两边卡在同一样东西——QQ 协议端。

| 部件 | 安卓 | iPad/iOS |
|---|---|---|
| Python + NoneBot2 | 可行（Termux 能装 CPython） | 不可行（没有可常驻的 Python 运行环境） |
| QQ 协议端（NapCat = 注入 QQ NT 桌面版） | 不可行（没有 QQ NT 桌面版） | 不可行 |
| GPT-SoVITS 语音、ComfyUI 生图 | 基本跑不动 | — |
| 电脑控制（本机 GUI / 截图） | 不支持（代码是 Windows ctypes） | 不支持 |
| 常驻后台 | Termux 会被系统杀，需要前台服务/掉电优化 | iOS 不允许长期后台 |

安卓理论上还有一条路：用 .NET 系的 Lagrange.Core 做协议端（Termux 里装 .NET 8），
**但我没有实测过**，而且那边生态已大不如前，风控更严。不建议把主力号押上去。

真正可行的形态：**PC 常开跑学姐，手机/平板只当显示器**——用
`http://<PC 局域网 IP>:端口` 打开后台，或用内网穿透。这也是这台机器现在的用法。

## 四、故意没做的

- **实时变声唱歌（RVC）**：训练+破音+要 VIP，投入产出比最低，语音已经有 GPT-SoVITS。
- **把整台电脑全权交给她**：电脑控制只做到「白名单 + 租约 + 限流」，没有做
  「她自己开游戏玩两小时」那套连招编排。
- **听歌感知（网易云 now-playing）**：用户明确说不需要。

## 五、踩坑记录（从原版抄来的，省得再踩）

1. **Watt Toolkit(Steam++) 会把 GitHub 域名劫持到 127.0.0.1**，装依赖全失败。
   装东西前先关它。
2. **路径不能有中文和空格**，Node 侧会找不到模块（学姐是 Python，但 NapCat 那侧同理）。
3. **中文输入不能用 adb shell input text**（这条只在做手机控制时才相关）：
   它会静默丢字，表现为「她以为打了」。要做就必须明确报错，不能悄悄失败。
4. **日志别只记成功的**：失败那次才是你要看的。
