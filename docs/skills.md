# 技能层（Skills）

给模型**按需**塞一页领域说明书。试点技能：`data/skills/ppt-design/`。

## 它是什么 / 不是什么

| 是 | 不是 |
|---|---|
| 一份 Markdown 说明书（怎么做这类任务） | 一个可执行的能力 |
| 命中才进提示，没命中零成本 | 常驻系统提示词 |
| 拼进本轮的 `user_payload`（与知识库/联网/链接正文同构） | 新增系统提示插槽 |

这条边界是刻意的：**技能一旦能执行，就会和工具层抢职责**，调试成本立刻翻倍。
需要动手的任务归 agent loop + `ToolRegistry`。

## 怎么加一个技能

1. 建目录 `data/skills/<名字>/`，写 `SKILL.md`：

```markdown
---
name: ppt-design
description: 做 PPT 的版式与配色规范
triggers: ppt, 幻灯片, 演示文稿, 汇报
---

正文：分小节写"做法"，不要写成百科。
```

2. `.env` 里打开总开关：`DL_SENPAI_SKILL_ENABLE=true`。
3. 重启 bot。

`triggers` 是**逗号分隔的触发词**，做大小写不敏感的子串匹配；
命中的词越多，排序越靠前（多个技能同时命中时取前 `DL_SENPAI_SKILL_MAX_SKILLS` 个）。

⚠️ 没有 `triggers` 的技能永远不会自动注入 —— 想常驻就写进人设。

## 渐进披露：为什么"装了 20 个技能"不等于"每条消息多 20 页"

- 目录扫描只解析 frontmatter（`name` / `description` / `triggers`），不读正文；
- 命中之后才读 `SKILL.md` 全文，并截断到 `DL_SENPAI_SKILL_MAX_CHARS`；
- 没命中就直接返回空串，`user_payload` 一个字符都不加。

## 配置

| 变量 | 默认 | 说明 |
|---|---|---|
| `DL_SENPAI_SKILL_ENABLE` | `false` | 总开关（新能力默认关） |
| `DL_SENPAI_SKILL_DIR` | `data/skills` | 技能根目录 |
| `DL_SENPAI_SKILL_MAX_CHARS` | `1500` | 单条正文注入上限（字） |
| `DL_SENPAI_SKILL_MAX_SKILLS` | `1` | 单条消息最多注入几个 |
| `DL_SENPAI_SKILL_HOT_RELOAD` | `true` | 改完 SKILL.md 立刻生效（不必重启） |

## 热重载

改 `SKILL.md` 不用重启 bot：每次注入前只比一次**文件指纹**（路径 + mtime_ns），
变了就重扫，没变就跳过。用 mtime 而不是内容哈希，是因为技能文档是手写小文件、
改动必然刷新 mtime；哈希要把所有正文读一遍，等于把懒加载的意义丢掉。

关掉它（`DL_SENPAI_SKILL_HOT_RELOAD=false`）适合"技能目录在网盘/同步盘上"的情况——
那种盘 mtime 会飘，热重载会反复重扫。

## 技能清单

`skills.catalog_text()` 输出「名字：说明 [触发: ...]」，启动时以 `skills.ready` 一行记进日志。
**故意不自动注入提示**：技能靠命中触发词才进上下文，清单常驻是纯开销。
等技能多到模型会漏判时，再把清单接进提示（渐进披露第一层）。
## 观测量

- `skill.injected name=... chars=...` —— 这一轮注入了哪个技能、多少字；
- `skill.empty` / `skill.unreadable` —— 某份技能文档不可用（被跳过，不连坐其他技能）；
- `skill.failed` —— 整个技能层出错（已降级为"不加材料"，回复照常走）。

## 不打算做的（写在这里防止以后又长出来）

- 技能内部调用工具 / 编排多步流程 → 那是 agent loop 的活；
- 后台热编辑技能文档（改完要重启，先别引入文件监听）；
- 技能之间互相调用或组合；
- 用向量检索选技能（当前是确定性触发词匹配：可回归、可解释、零额外调用）。
