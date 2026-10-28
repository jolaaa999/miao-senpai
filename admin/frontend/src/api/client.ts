import axios from 'axios'

const TOKEN_KEY = 'senpai_console_token'
const API_BASE = '/x7k9-dl-senpai-console/v1'

export const api = axios.create({
  baseURL: API_BASE,
  timeout: 30000,
})

api.interceptors.request.use((config) => {
  const token = localStorage.getItem(TOKEN_KEY)
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

api.interceptors.response.use(
  (res) => res,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem(TOKEN_KEY)
      if (!window.location.pathname.includes('/login')) {
        window.location.href = '/login'
      }
    }
    return Promise.reject(err)
  },
)

export function setToken(token: string) {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY)
}

export function hasToken() {
  return !!localStorage.getItem(TOKEN_KEY)
}

export interface SessionSummary {
  session_id: string
  session_type: string
  ref_id: string
  turn_count: number
  message_count: number
  updated_at: string
  preview: string
}

export interface ChatTurn {
  role: 'user' | 'assistant'
  content: string
}

export interface OverviewPulse {
  session_count: number
  checkin_group_count: number
  checkin_user_count: number
  affection_group_count: number
  affection_user_count: number
  sticker_count: number
  draw_count: number
  trends_item_count: number
  trends_fetched_at: string
  feature_count: number
  features_enabled: number
  recent_sessions: SessionSummary[]
  project_root: string
  speak_style_active_id?: string
  speak_style_active_name?: string
  speak_style_count?: number
}

export interface SpeakStyle {
  id: string
  name: string
  kind: string
  enabled: boolean
  model: string
  base_url: string
  api_key: string
  system_overlay: string
  source_person: string
  qq_data_note: string
  trained_model_ref: string
  notes: string
  updated_at: number
}

export interface SpeakStylesSnapshot {
  active_id: string
  active: SpeakStyle
  styles: SpeakStyle[]
  path: string
}

export interface CheckinUser {
  user_id: string
  display_name: string
  points: number
  streak: number
  max_streak: number
  last_checkin: string
  total_checkins: number
  title: string
  pending_task?: { id: string; text: string; reward: number; assigned_date: string }
}

export interface AffectionGroup {
  group_id: string
  scope_type: 'group' | 'private'
  user_count: number
  updated_at?: string
}

export interface AffectionUser {
  user_id: string
  display_name: string
  value: number
  tier_title: string
  tier_name: string
  next_tier_name?: string
  next_tier_threshold?: number
  points_to_next?: number
}

export interface StickerItem {
  id: string
  segment_type: string
  context_text: string
  keywords: string[]
  use_count: number
  collected_at: string
  source_user: string
  has_local_file: boolean
  local_path?: string
}

export interface DrawItem {
  id: string
  prompt: string
  scene: string
  user_request: string
  source_user: string
  source_user_id: string
  local_path?: string
  model: string
  size: string
  created_at: string
  has_local_file: boolean
}

export interface FeatureSwitch {
  key: string
  value: string
  group: string
  masked: boolean
  description?: string
}

export interface GroupFeatureCatalogItem {
  key: string
  label: string
}

// 可写开关。in_env_files 说明该键在哪些文件里声明过——
// 同时出现在 .env 与 .env.dev 时写入会覆盖两处，
// 否则 .env.dev 的旧值会把改动盖回去。
export interface EditableFeature {
  key: string
  value: string
  writable: boolean
  in_env_files: string[]
}

export interface GroupFeatureRow {
  group_id: string
  name: string
  features: Record<string, boolean | null>
  updated_at: string
}

export interface LogFileInfo {
  id: string
  name: string
  category: string
  rel_path: string
  size: number
  updated_at: string
}

export interface LogTailResult {
  id: string
  lines: string[]
  total: number
  tail: number
  truncated: boolean
}

export const authApi = {
  login: (username: string, password: string) =>
    api.post<{ token: string; username: string }>('/auth/token', { username, password }),
}

// 一行 agent.request_summary 的结构化视图。
//
// 字段与 bot 侧一致（见 src/plugins/dl_senpai/agent/entry.py）；
// raw 保留整行原文 —— 后端解析漏字段时还能自己看。
export interface AgentTrace {
  time: string
  session_id: string
  trace: string
  mode: string
  reply_by: string
  stop: string
  trigger: string
  total_ms: number
  queue_ms: number
  model_ms: number
  tool_ms: number
  send_ms: number
  model_calls: number
  usage: number
  tool_calls: number
  tools: string
  capabilities: string
  error: string
  file: string
  raw: string
}

export interface AgentTraceResult {
  items: AgentTrace[]
  scanned: number
  files: string[]
  notice: string
}

// ---------- 人设卡 ----------
// 一张卡 = 一份「她是谁」（data/dl_senpai/personas/*.md）。
// 绑定到某个会话后，那个会话就用卡；没绑定的继续用 persona.py 那份。
export interface PersonaCardInfo {
  id: string
  name: string
  description: string
  groups: string[]
  chars: number
  file: string
  source_ref: string
  /** null = 没有可对账的来源；true/false = 与 persona.py 同步 / 已漂移 */
  source_synced: boolean | null
  updated_at: string
}

export interface PersonaCardsSnapshot {
  dir: string
  cards: PersonaCardInfo[]
  /** 会话键（group:xxx / private:xxx）-> 卡 id */
  bindings: Record<string, string>
  default_id: string
  note: string
}

export interface PersonaCardBody {
  id: string
  name: string
  body: string
  file: string
}

export interface PersonaDraft {
  senpai_name: string
  system_prompt: string
  appearance: string
  monster_hunter_extra: string
  file_path: string
  updated_at: string
}

// ---------- 学姐日记（v1.3.0） ----------
export interface DiaryEntry {
  id: string
  day: string
  title: string
  text: string
  mood: string
  angle: string
  tier: string
  tags: string[]
  life_days: number
  created_at: string
}

export interface DiaryDay {
  day: string
  count: number
}

export interface DiarySnapshot {
  items: DiaryEntry[]
  days: DiaryDay[]
  total: number
  path: string
}

// ---------- 服装店（v1.3.0） ----------
export interface OutfitCatalogItem {
  id: string
  name: string
  price: number
  outfit_en: string
  /** 缩略图文件名；为空表示本地没有图 */
  thumb: string
}

export interface OutfitSlot {
  id: string
  name: string
}

export interface ShopGroup {
  group_id: string
  name: string
  slots: OutfitSlot[]
  current_outfit_id: string
  current_outfit_name: string
  slots_refresh_day: string
  updated_at: string
  warehouse_user_count: number
  owned_count: number
}

export interface OutfitShopSnapshot {
  catalog: OutfitCatalogItem[]
  groups: ShopGroup[]
  thumb_dir: string
  catalog_path: string
}

// ---------- 角色档案与身份一致性（v1.3.0） ----------
export type VerificationStatus = 'human_verified' | 'assistant_reviewed' | 'unverified'

export interface CharacterField {
  key: string
  label: string
  value: string
  verification_status: VerificationStatus | string
  verification_method: string
  verified_at: string
  note: string
}

export interface CharacterProfile {
  key: string
  canonical_name: string
  localized_names: string[]
  aliases: string[]
  franchise: string
  franchise_localized: string
  identity_priority: string
  confidence: number
  profile_status: string
  fields: CharacterField[]
  must_preserve: string[]
  creative_freedom: string[]
  negative_notes: string[]
  human_verified_count: number
  reviewed_count: number
  unverified_count: number
}

export interface CharacterReference {
  manifest: string
  character: string
  localized_name: string
  file: string
  type: string
  reference_role: string
  verification_status: string
  priority: number
  source_type: string
  verified_at: string
  notes: string
}

export interface IdentityBenchmark {
  strategy: string
  file: string
  item_count: number
  models: string[]
  avg_prompt_chars: number
  /** 带人工核验参考图的条数（部分覆盖也要能看见） */
  human_reference_count: number
  created_from: string
  created_to: string
}

export interface ImageTrace {
  trace_id: string
  character: string
  franchise: string
  intent: string
  reference_mode: string
  reference_count: number
  generation_mode: string
  result: string
  elapsed_ms: number
  final_prompt_chars: number
  original_prompt: string
  ts: string
}

export interface CharacterSnapshot {
  profiles: CharacterProfile[]
  references: CharacterReference[]
  benchmarks: IdentityBenchmark[]
  traces: ImageTrace[]
  note: string
  version: number
  catalog: string
}

export const consoleApi = {
  overview: () => api.get<OverviewPulse>('/overview-pulse'),
  sessions: () => api.get<{ items: SessionSummary[] }>('/senpai-sessions'),
  turns: (sid: string) => api.get<{ session_id: string; turns: ChatTurn[] }>(`/senpai-sessions/${encodeURIComponent(sid)}/turns`),
  clearSession: (sid: string) => api.delete(`/senpai-sessions/${encodeURIComponent(sid)}/turns`),
  checkinGroups: () => api.get<{ items: { group_id: string; user_count: number }[] }>('/checkin-roster'),
  checkinUsers: (gid: string) => api.get<{ items: CheckinUser[] }>(`/checkin-roster/${gid}/users`),
  patchCheckinUser: (gid: string, uid: string, data: Partial<CheckinUser>) =>
    api.patch(`/checkin-roster/${gid}/users/${uid}`, data),
  affectionGroups: () => api.get<{ items: AffectionGroup[] }>('/affection-roster'),
  affectionUsers: (gid: string) => api.get<{ items: AffectionUser[] }>(`/affection-roster/${gid}/users`),
  patchAffectionUser: (gid: string, uid: string, data: Partial<AffectionUser>) =>
    api.patch(`/affection-roster/${gid}/users/${uid}`, data),
  stickerSessions: () => api.get<{ items: { session_id: string; item_count: number }[] }>('/sticker-vault'),
  stickerItems: (sid: string) => api.get<{ items: StickerItem[] }>(`/sticker-vault/${encodeURIComponent(sid)}/items`),
  stickerAsset: (filename: string) =>
    api.get<Blob>(`/sticker-vault/assets/${encodeURIComponent(filename)}`, { responseType: 'blob' }),
  deleteSticker: (sid: string, id: string) => api.delete(`/sticker-vault/${encodeURIComponent(sid)}/items/${id}`),
  drawSessions: () => api.get<{ items: { session_id: string; item_count: number }[] }>('/draw-gallery'),
  drawItems: (sid: string) => api.get<{ items: DrawItem[] }>(`/draw-gallery/${encodeURIComponent(sid)}/items`),
  drawAsset: (filename: string) =>
    api.get<Blob>(`/draw-gallery/assets/${encodeURIComponent(filename)}`, { responseType: 'blob' }),
  deleteDraw: (sid: string, id: string) => api.delete(`/draw-gallery/${encodeURIComponent(sid)}/items/${id}`),
  features: () => api.get<{ items: FeatureSwitch[] }>('/feature-switches'),
  // 可写开关（只含布尔项）；PATCH 会同时写 .env 与 .env.dev，
  // bot 侧 config_watcher 数秒内热重载生效，无需重启。
  editableFeatures: () => api.get<{ items: EditableFeature[] }>('/editable-feature-switches'),
  setFeature: (key: string, value: string) =>
    api.patch<{ key: string; value: string; written_to: string[]; note: string }>(
      `/feature-switches/${encodeURIComponent(key)}`,
      { value },
    ),
  groupFeatures: () =>
    api.get<{ items: GroupFeatureRow[]; features: GroupFeatureCatalogItem[] }>('/group-features'),
  patchGroupFeature: (gid: string, feature: string, value: boolean | null) =>
    api.patch<GroupFeatureRow>(`/group-features/${encodeURIComponent(gid)}`, { feature, value }),
  trends: () => api.get<{ items: { source: string; title: string; rank: number; hot: string }[]; fetched_at: string }>('/trends-snapshot'),
  logStreams: () => api.get<{ items: LogFileInfo[] }>('/log-streams'),
  logTail: (id: string, tail = 300) =>
    api.get<LogTailResult>(`/log-streams/${encodeURIComponent(id)}/lines`, { params: { tail } }),
  agentTraces: (params: { session_id?: string; keyword?: string; reply_by?: string; mode?: string; limit?: number }) =>
    api.get<AgentTraceResult>('/agent-traces', { params }),
  personaDraft: () => api.get<PersonaDraft>('/senpai-persona-draft'),
  savePersonaDraft: (data: Partial<PersonaDraft>) => api.put<PersonaDraft>('/senpai-persona-draft', data),
  // 人设卡：列 / 绑（card 为空即解绑）/ 看正文 / 从 persona.py 导出一张
  personaCards: () => api.get<PersonaCardsSnapshot>('/persona-cards'),
  bindPersonaCard: (session: string, card: string) =>
    api.put<PersonaCardsSnapshot>('/persona-cards/binding', { session, card }),
  personaCardBody: (id: string) =>
    api.get<PersonaCardBody>(`/persona-cards/${encodeURIComponent(id)}/body`),
  exportPersonaCard: (id: string) =>
    api.post<PersonaCardsSnapshot>('/persona-cards/export', { id }),
  speakStyles: () => api.get<SpeakStylesSnapshot>('/speak-styles'),
  setSpeakStyleActive: (id: string) => api.put<SpeakStylesSnapshot>('/speak-styles/active', { id }),
  upsertSpeakStyle: (data: Partial<SpeakStyle> & { id: string; name: string }) =>
    api.put<SpeakStylesSnapshot>('/speak-styles', data),
  deleteSpeakStyle: (id: string) => api.delete<SpeakStylesSnapshot>(`/speak-styles/${encodeURIComponent(id)}`),
  // v1.3.0 新增：学姐日记 / 服装店 / 角色身份
  diary: (limit = 200) => api.get<DiarySnapshot>('/senpai-diary', { params: { limit } }),
  outfitShop: () => api.get<OutfitShopSnapshot>('/outfit-shop'),
  shopThumb: (filename: string) =>
    api.get<Blob>(`/outfit-shop/thumbs/${encodeURIComponent(filename)}`, { responseType: 'blob' }),
  characterIdentity: (traceLimit = 30) =>
    api.get<CharacterSnapshot>('/character-identity', { params: { trace_limit: traceLimit } }),
}
