<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { consoleApi, type OverviewPulse } from '@/api/client'
import { useRouter } from 'vue-router'

const router = useRouter()
const data = ref<OverviewPulse | null>(null)
const loading = ref(true)

const stats = [
  { key: 'session_count', label: '聊天 Session', icon: '💬', color: '#f97316' },
  { key: 'checkin_user_count', label: '签到用户', icon: '📅', color: '#10b981' },
  { key: 'affection_user_count', label: '好感档案', icon: '💕', color: '#fb7185' },
  { key: 'sticker_count', label: '收藏表情', icon: '😺', color: '#f59e0b' },
  { key: 'draw_count', label: '生图归档', icon: '🎨', color: '#a855f7' },
  { key: 'trends_item_count', label: '热榜条目', icon: '🔥', color: '#38bdf8' },
] as const

onMounted(async () => {
  try {
    const res = await consoleApi.overview()
    data.value = res.data
  } finally {
    loading.value = false
  }
})

function formatTime(iso: string) {
  if (!iso || iso.startsWith('0001')) return '—'
  return new Date(iso).toLocaleString('zh-CN')
}

function sessionLabel(s: { session_type: string; ref_id: string }) {
  return s.session_type === 'group' ? `群 ${s.ref_id}` : `私聊 ${s.ref_id}`
}
</script>

<template>
  <div v-loading="loading">
    <h1 class="page-title">总览</h1>
    <p class="page-subtitle">学姐运行状态一瞥 · <span class="root-path">{{ data?.project_root }}</span></p>

    <div class="stat-grid">
      <div v-for="s in stats" :key="s.key" class="stat-card glass-card">
        <span class="stat-icon">{{ s.icon }}</span>
        <div>
          <div class="stat-value" :style="{ color: s.color }">{{ data?.[s.key] ?? '—' }}</div>
          <div class="stat-label">{{ s.label }}</div>
        </div>
      </div>
      <div class="stat-card glass-card clickable" @click="router.push('/speak-styles')">
        <span class="stat-icon">🗣️</span>
        <div>
          <div class="stat-value" style="color: #6366f1; font-size: 18px">
            {{ data?.speak_style_active_name || '默认学姐' }}
          </div>
          <div class="stat-label">
            当前语言风格
            <span v-if="data?.speak_style_count">· 共 {{ data.speak_style_count }} 套</span>
          </div>
        </div>
      </div>
    </div>

    <div class="grid-2">
      <section class="glass-card section section-sessions">
        <h2>最近活跃 Session</h2>
        <div class="session-list">
          <div v-if="!data?.recent_sessions?.length" class="empty">暂无聊天记录</div>
          <div
            v-for="s in data?.recent_sessions"
            :key="s.session_id"
            class="session-item"
            @click="router.push({ name: 'sessions', query: { sid: s.session_id } })"
          >
            <div class="session-meta">
              <el-tag size="small" :type="s.session_type === 'group' ? 'warning' : 'success'">
                {{ sessionLabel(s) }}
              </el-tag>
              <span class="time">{{ formatTime(s.updated_at) }}</span>
            </div>
            <div class="preview">{{ s.preview || '（空）' }}</div>
          </div>
        </div>
      </section>

      <section class="glass-card section section-features">
        <h2>功能概况</h2>
        <div class="feature-summary">
          <div class="ring">
            <span class="ring-num">{{ data?.features_enabled ?? 0 }}</span>
            <span class="ring-label">已启用</span>
          </div>
          <div class="feature-detail">
            <p>共 <strong>{{ data?.feature_count ?? 0 }}</strong> 项 DL_SENPAI / LLM 配置</p>
            <p>热榜缓存：{{ formatTime(data?.trends_fetched_at ?? '') }}</p>
            <p>签到群：{{ data?.checkin_group_count ?? 0 }} 个</p>
            <p>好感群：{{ data?.affection_group_count ?? 0 }} 个</p>
            <el-button class="feature-link" text type="primary" @click="router.push('/features')">
              查看全部 →
            </el-button>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped lang="scss">
.stat-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 1rem;
  margin-bottom: 1.5rem;

  @media (max-width: 900px) {
    grid-template-columns: repeat(2, 1fr);
  }

  @media (max-width: 560px) {
    grid-template-columns: 1fr;
  }
}

.stat-card {
  display: flex;
  align-items: center;
  gap: 1rem;
  padding: 1.25rem;
}

.stat-card.clickable {
  cursor: pointer;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.stat-card.clickable:hover {
  border-color: var(--el-color-primary);
  box-shadow: 0 0 0 1px var(--el-color-primary-light-7);
}

.stat-icon {
  font-size: 2rem;
}

.stat-value {
  font-size: 1.75rem;
  font-weight: 700;
  line-height: 1.2;
}

.stat-label {
  color: var(--senpai-muted);
  font-size: 0.85rem;
}

.page-subtitle .root-path {
  word-break: break-all;
}

.grid-2 {
  display: grid;
  grid-template-columns: minmax(0, 1.4fr) minmax(280px, 1fr);
  gap: 1rem;
  align-items: stretch;

  @media (max-width: 900px) {
    grid-template-columns: 1fr;
  }
}

.section {
  display: flex;
  flex-direction: column;
  padding: 1.25rem;
  min-height: 0;

  h2 {
    margin: 0 0 1rem;
    font-size: 1rem;
    color: var(--senpai-muted);
    font-weight: 600;
    flex-shrink: 0;
  }
}

.session-list {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  min-height: 120px;
}

.session-item {
  padding: 0.75rem;
  border-radius: 10px;
  cursor: pointer;
  transition: background 0.2s;

  &:hover {
    background: var(--senpai-accent-soft);
  }
}

.session-meta {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.35rem;

  .time {
    font-size: 0.75rem;
    color: var(--senpai-muted);
  }
}

.preview {
  font-size: 0.85rem;
  color: var(--senpai-text);
  opacity: 0.85;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.empty {
  color: var(--senpai-muted);
  font-size: 0.9rem;
}

.feature-summary {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 1.5rem;
  padding: 0.5rem 0;
  min-height: 160px;

  @media (max-width: 1100px) {
    flex-direction: column;
    text-align: center;
  }
}

.ring {
  flex-shrink: 0;
  width: 108px;
  height: 108px;
  border-radius: 50%;
  border: 3px solid var(--senpai-accent);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  box-shadow: 0 0 24px var(--senpai-accent-glow);
}

.ring-num {
  font-size: 1.75rem;
  font-weight: 700;
  color: var(--senpai-accent);
}

.ring-label {
  font-size: 0.7rem;
  color: var(--senpai-muted);
}

.feature-detail {
  display: flex;
  flex-direction: column;
  justify-content: center;
  gap: 0.45rem;
  min-width: 0;
  flex: 1;
  max-width: 280px;

  p {
    margin: 0;
    font-size: 0.9rem;
    line-height: 1.45;
    color: var(--senpai-muted);

    strong {
      color: var(--senpai-text);
    }
  }
}

.feature-link {
  align-self: flex-start;
  margin-top: 0.25rem;
  padding-left: 0 !important;

  @media (max-width: 1100px) {
    align-self: center;
  }
}
</style>
