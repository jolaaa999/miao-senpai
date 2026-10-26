<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { consoleApi, type AgentTrace } from '@/api/client'

// 过滤条件全部留在前端状态里：查询是无副作用的纯读接口，
// 不需要把状态同步到后端（也就没有"刷新后筛选丢失"这类问题）。
const rows = ref<AgentTrace[]>([])
const scanned = ref(0)
const files = ref<string[]>([])
const notice = ref('')
const loading = ref(false)
const sessionId = ref('')
const keyword = ref('')
const replyBy = ref('')
const mode = ref('')
const limit = ref(200)
const autoRefresh = ref(false)

let timer: ReturnType<typeof setInterval> | null = null

const slowCount = computed(() => rows.value.filter((r) => r.total_ms >= 8000).length)
const errorCount = computed(() => rows.value.filter((r) => r.error).length)
const fallbackCount = computed(
  () => rows.value.filter((r) => r.reply_by === 'safe_fallback' || r.reply_by === 'legacy_fallback').length,
)

// 回复来源直接决定"这条到底是谁生成的"，所以给它上色：
// runtime 正常、兜底要显眼、什么都没生成（none）更要显眼。
function replyTagType(v: string) {
  if (v === 'runtime') return 'success'
  if (v === 'legal' || v === 'legacy') return 'info'
  if (v === 'legacy_fallback') return 'warning'
  if (v === 'safe_fallback') return 'danger'
  return 'info'
}

function durationClass(v: number) {
  if (v >= 8000) return 'dur-slow'
  if (v >= 3000) return 'dur-warn'
  return ''
}

async function load() {
  loading.value = true
  try {
    const res = await consoleApi.agentTraces({
      session_id: sessionId.value.trim() || undefined,
      keyword: keyword.value.trim() || undefined,
      reply_by: replyBy.value || undefined,
      mode: mode.value || undefined,
      limit: limit.value,
    })
    rows.value = res.data.items ?? []
    scanned.value = res.data.scanned ?? 0
    files.value = res.data.files ?? []
    notice.value = res.data.notice ?? ''
  } finally {
    loading.value = false
  }
}

function setupAutoRefresh() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
  if (autoRefresh.value) {
    timer = setInterval(load, 5000)
  }
}

function toggleAuto() {
  autoRefresh.value = !autoRefresh.value
  setupAutoRefresh()
}

onMounted(load)
onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div>
    <h1 class="page-title">调用链</h1>
    <p class="page-subtitle">
      每条消息一行 <code>agent.request_summary</code>：走了哪条路、为什么停、调了什么工具、
      花了多少 token、最后是谁生成的回复。数据直接读 <code>data/logs/bot-*.log</code>，
      <strong>只含元数据</strong>（不含用户原话与回复正文）。
    </p>

    <div class="glass-card filter-bar">
      <el-input v-model="sessionId" placeholder="会话 ID（group:123456）" clearable class="w220" @keyup.enter="load" />
      <el-input v-model="keyword" placeholder="关键词：工具名 / trace / 错误" clearable class="w220" @keyup.enter="load" />
      <el-select v-model="replyBy" placeholder="回复来源" clearable class="w160">
        <el-option label="runtime（正常运行）" value="runtime" />
        <el-option label="legacy_fallback（旧路径降级）" value="legacy_fallback" />
        <el-option label="safe_fallback（兜底文案）" value="safe_fallback" />
        <el-option label="none（没生成）" value="none" />
      </el-select>
      <el-select v-model="mode" placeholder="模式" clearable class="w140">
        <el-option label="runtime" value="runtime" />
        <el-option label="shadow" value="shadow" />
        <el-option label="canary" value="canary" />
        <el-option label="legacy" value="legacy" />
      </el-select>
      <el-select v-model="limit" class="w120">
        <el-option :label="'最近 100 条'" :value="100" />
        <el-option :label="'最近 200 条'" :value="200" />
        <el-option :label="'最近 500 条'" :value="500" />
      </el-select>
      <el-button type="primary" :loading="loading" @click="load">查询</el-button>
      <el-button :type="autoRefresh ? 'success' : 'default'" @click="toggleAuto">
        {{ autoRefresh ? '自动刷新中' : '自动刷新' }}
      </el-button>
    </div>

    <div class="stat-row">
      <span class="stat">命中 <strong>{{ rows.length }}</strong> 条</span>
      <span class="stat">扫描 <strong>{{ scanned }}</strong> 行</span>
      <span class="stat">慢请求（≥8s）<strong class="warn">{{ slowCount }}</strong></span>
      <span class="stat">兜底 <strong class="warn">{{ fallbackCount }}</strong></span>
      <span class="stat">带错误 <strong class="danger">{{ errorCount }}</strong></span>
      <span class="stat files">文件：{{ files.join(' / ') || '—' }}</span>
    </div>

    <el-alert v-if="notice" :title="notice" type="info" :closable="false" class="notice" />

    <div v-loading="loading" class="glass-card table-wrap">
      <el-table :data="rows" size="small" height="calc(100vh - 340px)">
        <el-table-column prop="time" label="时间" width="140" />
        <el-table-column prop="session_id" label="会话" width="150" show-overflow-tooltip />
        <el-table-column prop="mode" label="模式" width="90" />
        <el-table-column label="回复来源" width="150">
          <template #default="{ row }">
            <el-tag size="small" :type="replyTagType(row.reply_by)">{{ row.reply_by || '—' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="stop" label="停止原因" width="130" show-overflow-tooltip />
        <el-table-column label="耗时" width="110">
          <template #default="{ row }">
            <span :class="durationClass(row.total_ms)">{{ Math.round(row.total_ms) }} ms</span>
          </template>
        </el-table-column>
        <el-table-column label="模型/工具" width="120">
          <template #default="{ row }">{{ row.model_calls }} 次 / {{ row.tool_calls }} 个</template>
        </el-table-column>
        <el-table-column prop="usage" label="token" width="90" />
        <el-table-column prop="tools" label="工具明细" min-width="220" show-overflow-tooltip />
        <el-table-column label="错误" min-width="140" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="danger">{{ row.error }}</span>
          </template>
        </el-table-column>
        <el-table-column type="expand" width="40">
          <template #default="{ row }">
            <pre class="raw">{{ row.raw }}</pre>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<style scoped lang="scss">
.filter-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem;
  align-items: center;
  padding: 0.9rem 1rem;
  margin-bottom: 0.75rem;
}

.w220 { width: 220px; }
.w160 { width: 200px; }
.w140 { width: 140px; }
.w120 { width: 130px; }

.stat-row {
  display: flex;
  flex-wrap: wrap;
  gap: 1.2rem;
  align-items: center;
  padding: 0 0.25rem 0.6rem;
  font-size: 0.85rem;
  color: var(--senpai-muted);
}

.stat.files { margin-left: auto; font-size: 0.78rem; }
.warn { color: #f0a020; }
.danger { color: #f87171; }

.dur-warn { color: #f0a020; }
.dur-slow { color: #f87171; font-weight: 600; }

.notice { margin-bottom: 0.75rem; }

.table-wrap { padding: 0.5rem; }

.raw {
  margin: 0;
  padding: 0.6rem 0.8rem;
  white-space: pre-wrap;
  word-break: break-all;
  font-size: 0.78rem;
  color: var(--senpai-muted);
}
</style>
