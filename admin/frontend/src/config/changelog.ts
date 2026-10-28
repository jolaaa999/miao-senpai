/**
 * 控制台版本与更新记录。
 *
 * 约定：控制台只要有用户看得见的改动（新页面 / 新接口 / 交互变化），就在这里加一条，
 * 版本号走语义化版本（major.minor.patch）：
 *   major —— 控制台整体重做（布局、鉴权、数据源变更）
 *   minor —— 新增页面或新增可写能力
 *   patch —— 修 bug、改文案、调样式
 * 数组按时间倒序排列，CONSOLE_VERSION 直接取最新一条，避免两处写版本号写岔。
 */

export interface ChangelogPage {
  /** 页面名（与控制台左侧导航一致） */
  name: string
  /** 这个页面是新增还是调整 */
  kind: '新增' | '调整'
  desc: string
}

export interface ChangelogEntry {
  version: string
  date: string
  title: string
  /** 本版最值得看的几条 */
  highlights: string[]
  /** 页面级变更清单 */
  pages: ChangelogPage[]
  /** 对应的接口 / 数据源，方便排查 */
  api?: string[]
  /** 关联 commit（有就写，方便回溯） */
  commits?: string[]
}

export const CHANGELOG: ChangelogEntry[] = [
  {
    version: '1.3.0',
    date: '2026-10-28',
    title: '接上「学姐的日常」与身份一致性这条线',
    highlights: [
      '新增学姐日记页：看 own_life 刷视频后写下的短记，可按天筛选、看标签分布',
      '新增角色档案页：视觉特征三态核验（人工核验 / 模型审阅 / 未核验）、参考图清单、A/B 实验与最近生图溯源',
      '新增服装店页：总目录 28 件衣服 + 每个群的今日货架、当前穿着与已购统计，缩略图直接预览',
      '新增热梗缓存页：热榜来源、排名、热度与抓取时间（此前接口已有、页面没有）',
      '新增更新记录页（本页）：每次改动都写清版本号、日期与改了什么',
    ],
    pages: [
      { name: '学姐日记', kind: '新增', desc: '读 data/dl_senpai/diary/index.jsonl，按天汇总 + 标签筛选' },
      { name: '角色档案', kind: '新增', desc: '读 characters.json 与参考图 manifest，汇总核验状态与生图溯源' },
      { name: '服装店', kind: '新增', desc: '读 shop/catalog.json 与各群 group_*.json，缩略图由后端受限转发' },
      { name: '热梗缓存', kind: '新增', desc: '复用 /trends-snapshot，展示来源 / 排名 / 热度' },
      { name: '更新记录', kind: '新增', desc: '版本号、日期、变更清单（本页）' },
      { name: '左侧导航', kind: '调整', desc: '新增 5 个入口，并在底部显示当前控制台版本号' },
    ],
    api: [
      'GET /senpai-diary?limit=200',
      'GET /outfit-shop',
      'GET /outfit-shop/thumbs/:name',
      'GET /character-identity?trace_limit=30',
    ],
  },
  {
    version: '1.2.0',
    date: '2026-10-26',
    title: '调用链只读视图',
    highlights: [
      '新增调用链页：把日志里的 agent.request_summary 解析成结构化表格',
      '可按 session / 关键词 / 回复来源 / 模式过滤，看到排队、模型、工具、发送各阶段耗时',
    ],
    pages: [
      { name: '调用链', kind: '新增', desc: '读运行日志中的 request_summary 行，只读' },
      { name: '功能开关', kind: '调整', desc: '补充技能层相关配置项说明' },
    ],
    api: ['GET /agent-traces'],
    commits: ['044093c'],
  },
  {
    version: '1.1.0',
    date: '2026-10-15',
    title: '功能开关可写 + 热重载',
    highlights: [
      '开关从只读变成可写：PATCH 同时写 .env 与 .env.dev，bot 侧 config_watcher 数秒内热重载',
      '面板上标明某个键出现在哪些 env 文件，避免改了 .env 又被 .env.dev 的旧值盖回去',
      'API Key 类配置保持脱敏显示',
    ],
    pages: [{ name: '功能开关', kind: '调整', desc: '布尔开关可点选保存，写入后提示生效位置' }],
    api: ['GET /editable-feature-switches', 'PATCH /feature-switches/:key'],
    commits: ['54acacb'],
  },
  {
    version: '1.0.0',
    date: '2026-09-12',
    title: '控制台初版',
    highlights: [
      'Go + Gin 后端（127.0.0.1:28473，前缀 /x7k9-dl-senpai-console/v1）+ Vue3 前端（41788）',
      '直接读写 data/dl_senpai/ 下的记忆、签到、好感、表情、生图、热榜 JSON，与 bot 同源',
      'JWT 登录，token 存 localStorage',
    ],
    pages: [
      { name: '总览', kind: '新增', desc: 'session / 签到 / 好感 / 表情 / 生图 / 热梗统计卡片' },
      { name: '聊天记录', kind: '新增', desc: '按 session 看气泡对话，可清空某个 session 的记忆' },
      { name: '签到管理', kind: '新增', desc: '积分、连签、称号、每日任务，支持改分' },
      { name: '好感管理', kind: '新增', desc: '好感值档位与升级差额' },
      { name: '表情库', kind: '新增', desc: '按 session 浏览、预览、删除表情' },
      { name: '生图库', kind: '新增', desc: '生图归档与提示词回溯' },
      { name: '学姐自设', kind: '新增', desc: '人设草稿编辑与保存' },
      { name: '语言风格', kind: '新增', desc: '多套说话风格切换与编辑' },
      { name: '功能开关', kind: '新增', desc: '解析 .env 的 DL_SENPAI_* 配置（初版只读）' },
      { name: '群功能开关', kind: '新增', desc: '按群覆盖功能的开关矩阵' },
    ],
    commits: ['bb574ff'],
  },
]

/** 当前控制台版本：始终等于最新一条记录，避免手改漏改 */
export const CONSOLE_VERSION = CHANGELOG[0]?.version ?? '0.0.0'
