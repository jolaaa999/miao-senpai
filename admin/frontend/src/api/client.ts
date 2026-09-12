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

export interface PersonaDraft {
  senpai_name: string
  system_prompt: string
  appearance: string
  monster_hunter_extra: string
  file_path: string
  updated_at: string
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
  groupFeatures: () =>
    api.get<{ items: GroupFeatureRow[]; features: GroupFeatureCatalogItem[] }>('/group-features'),
  patchGroupFeature: (gid: string, feature: string, value: boolean | null) =>
    api.patch<GroupFeatureRow>(`/group-features/${encodeURIComponent(gid)}`, { feature, value }),
  trends: () => api.get<{ items: { source: string; title: string; rank: number; hot: string }[]; fetched_at: string }>('/trends-snapshot'),
  logStreams: () => api.get<{ items: LogFileInfo[] }>('/log-streams'),
  logTail: (id: string, tail = 300) =>
    api.get<LogTailResult>(`/log-streams/${encodeURIComponent(id)}/lines`, { params: { tail } }),
  personaDraft: () => api.get<PersonaDraft>('/senpai-persona-draft'),
  savePersonaDraft: (data: Partial<PersonaDraft>) => api.put<PersonaDraft>('/senpai-persona-draft', data),
  speakStyles: () => api.get<SpeakStylesSnapshot>('/speak-styles'),
  setSpeakStyleActive: (id: string) => api.put<SpeakStylesSnapshot>('/speak-styles/active', { id }),
  upsertSpeakStyle: (data: Partial<SpeakStyle> & { id: string; name: string }) =>
    api.put<SpeakStylesSnapshot>('/speak-styles', data),
  deleteSpeakStyle: (id: string) => api.delete<SpeakStylesSnapshot>(`/speak-styles/${encodeURIComponent(id)}`),
}
