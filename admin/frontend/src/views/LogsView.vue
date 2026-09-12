<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { consoleApi, type LogFileInfo } from '@/api/client'

const files = ref<LogFileInfo[]>([])
const selectedId = ref('')
const lines = ref<string[]>([])
const truncated = ref(false)
const loadingList = ref(true)
const loadingContent = ref(false)
const filter = ref('')
const keyword = ref('')
const tailLines = ref(500)
const autoRefresh = ref(true)
const scrollRef = ref<HTMLElement | null>(null)

let timer: ReturnType<typeof setInterval> | null = null
let refreshTick = 0

const filteredFiles = computed(() => {
  const q = filter.value.trim().toLowerCase()
  if (!q) return files.value
  return files.value.filter(
    (f) =>
      f.name.toLowerCase().includes(q) ||
      f.category.toLowerCase().includes(q) ||
      f.rel_path.toLowerCase().includes(q),
  )
})

const displayLines = computed(() => {
  const q = keyword.value.trim().toLowerCase()
  if (!q) return lines.value
  return lines.value.filter((l) => l.toLowerCase().includes(q))
})

const selectedFile = computed(() => files.value.find((f) => f.id === selectedId.value))

function formatSize(n: number) {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

function formatTime(iso: string) {
  return new Date(iso).toLocaleString('zh-CN')
}

function lineClass(line: string) {
  const upper = line.toUpperCase()
  if (upper.includes('ERROR') || upper.includes('CRITICAL') || upper.includes('TRACEBACK')) {
    return 'level-error'
  }
  if (upper.includes('WARN') || upper.includes('WARNING')) {
    return 'level-warn'
  }
  if (upper.includes('INFO') || upper.includes('SUCCESS')) {
    return 'level-info'
  }
  return ''
}

function stripAnsi(text: string) {
  return text.replace(/\x1b\[[0-9;]*m/g, '')
}

function categoryLabel(cat: string) {
  const map: Record<string, string> = {
    bot: 'Bot 运行',
    'bot-错误': 'Bot 错误',
    napcat: 'NapCat',
    '语音 TTS': '语音 TTS',
  }
  return map[cat] ?? cat
}

async function loadFiles() {
  loadingList.value = true
  try {
    const res = await consoleApi.logStreams()
    files.value = res.data.items
    const botLog = files.value.find(
      (f) => f.name.startsWith('bot-') && !f.name.includes('.err'),
    )
    if (botLog) {
      selectedId.value = botLog.id
    } else if (!selectedId.value && files.value.length) {
      selectedId.value = files.value[0].id
    }
  } finally {
    loadingList.value = false
  }
}

async function loadContent(silent = false) {
  if (!selectedId.value) return
  if (!silent) loadingContent.value = true
  try {
    const res = await consoleApi.logTail(selectedId.value, tailLines.value)
    lines.value = res.data.lines.map(stripAnsi)
    truncated.value = res.data.truncated
    await nextTick()
    if (scrollRef.value) {
      scrollRef.value.scrollTop = scrollRef.value.scrollHeight
    }
  } finally {
    if (!silent) loadingContent.value = false
  }
}

function setupAutoRefresh() {
  if (timer) {
    clearInterval(timer)
    timer = null
  }
  if (autoRefresh.value) {
    refreshTick = 0
    timer = setInterval(async () => {
      await loadContent(true)
      refreshTick += 1
      if (refreshTick % 10 === 0) {
        await loadFiles()
      }
    }, 3000)
  }
}

onMounted(async () => {
  await loadFiles()
  await loadContent()
  setupAutoRefresh()
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})

watch(selectedId, () => loadContent())
watch(tailLines, () => loadContent())
watch(autoRefresh, () => setupAutoRefresh())
</script>

<template>
  <div>
    <h1 class="page-title">运行日志</h1>
    <p class="page-subtitle">
      查看 bot、NapCat、GPT-SoVITS 运行日志。Bot 日志写入
      <code>data/logs/bot-YYYYMMDD.log</code>，内容与终端一致。
      <strong>修改后需重启 bot</strong>（<code>python bot.py</code>）才会开始落盘。
    </p>

    <div class="logs-layout">
      <aside class="file-panel glass-card">
        <el-input v-model="filter" placeholder="搜索日志文件…" clearable class="search" />
        <div v-loading="loadingList" class="file-list">
          <div
            v-for="f in filteredFiles"
            :key="f.id"
            class="file-row"
            :class="{ active: f.id === selectedId }"
            @click="selectedId = f.id"
          >
            <div class="file-top">
              <el-tag size="small" type="info">{{ categoryLabel(f.category) }}</el-tag>
              <span class="size">{{ formatSize(f.size) }}</span>
            </div>
            <div class="file-name">{{ f.name }}</div>
            <div class="file-time">{{ formatTime(f.updated_at) }}</div>
          </div>
          <div v-if="!filteredFiles.length && !loadingList" class="empty">暂无日志文件</div>
        </div>
      </aside>

      <section class="log-panel glass-card">
        <div class="log-toolbar">
          <div class="log-meta" v-if="selectedFile">
            <code>{{ selectedFile.rel_path }}</code>
            <span v-if="truncated" class="trunc-hint">（仅显示尾部 {{ tailLines }} 行）</span>
          </div>
          <div class="log-actions">
            <el-input
              v-model="keyword"
              placeholder="过滤关键字…"
              clearable
              size="small"
              style="width: 160px"
            />
            <el-select v-model="tailLines" size="small" style="width: 110px">
              <el-option :value="100" label="100 行" />
              <el-option :value="300" label="300 行" />
              <el-option :value="500" label="500 行" />
              <el-option :value="1000" label="1000 行" />
            </el-select>
            <el-switch v-model="autoRefresh" active-text="自动刷新" size="small" />
            <el-button size="small" :loading="loadingContent" @click="loadContent">刷新</el-button>
          </div>
        </div>

        <div ref="scrollRef" v-loading="loadingContent" class="log-viewer">
          <div v-if="!displayLines.length && !loadingContent" class="empty-viewer">
            <template v-if="selectedFile?.name.startsWith('bot-')">
              暂无 Bot 日志。请先<strong>重启 bot</strong>，新进程会把终端输出写入此文件。
            </template>
            <template v-else>（空日志）</template>
          </div>
          <div v-for="(line, i) in displayLines" :key="i" class="log-line" :class="lineClass(line)">
            <span class="ln">{{ i + 1 }}</span>
            <span class="txt">{{ line || ' ' }}</span>
          </div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped lang="scss">
.logs-layout {
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 1rem;
  height: calc(100vh - 140px);
  min-height: 480px;

  @media (max-width: 800px) {
    grid-template-columns: 1fr;
    height: auto;
  }
}

.file-panel {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.search {
  margin: 1rem 1rem 0.5rem;
}

.file-list {
  flex: 1;
  overflow-y: auto;
  padding: 0 0.5rem 0.5rem;
}

.file-row {
  padding: 0.65rem 0.75rem;
  border-radius: 10px;
  cursor: pointer;
  margin-bottom: 0.25rem;
  border: 1px solid transparent;

  &:hover {
    background: var(--senpai-surface-hover);
  }

  &.active {
    background: var(--senpai-accent-soft);
    border-color: var(--senpai-border);
  }
}

.file-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.3rem;

  .size {
    font-size: 0.7rem;
    color: var(--senpai-muted);
  }
}

.file-name {
  font-size: 0.82rem;
  font-weight: 500;
  word-break: break-all;
}

.file-time {
  font-size: 0.7rem;
  color: var(--senpai-muted);
  margin-top: 0.2rem;
}

.log-panel {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.log-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.75rem;
  padding: 0.75rem 1rem;
  border-bottom: 1px solid var(--senpai-border);
}

.log-meta code {
  font-size: 0.78rem;
  color: var(--senpai-accent-deep);
}

.trunc-hint {
  font-size: 0.75rem;
  color: var(--senpai-muted);
  margin-left: 0.5rem;
}

.log-actions {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.log-viewer {
  flex: 1;
  overflow: auto;
  padding: 0.75rem 0;
  font-family: 'Cascadia Code', 'Consolas', 'DM Sans', monospace;
  font-size: 0.78rem;
  line-height: 1.55;
  background: rgba(255, 247, 237, 0.75);
}

.log-line {
  display: flex;
  gap: 0.75rem;
  padding: 0 1rem;
  white-space: pre-wrap;
  word-break: break-all;

  &:hover {
    background: rgba(249, 115, 22, 0.06);
  }

  .ln {
    flex-shrink: 0;
    width: 2.5rem;
    text-align: right;
    color: var(--senpai-muted);
    opacity: 0.6;
    user-select: none;
  }

  .txt {
    flex: 1;
    color: var(--senpai-text);
  }

  &.level-error .txt {
    color: #dc2626;
  }

  &.level-warn .txt {
    color: #d97706;
  }

  &.level-info .txt {
    color: #0284c7;
  }
}

.empty,
.empty-viewer {
  text-align: center;
  color: var(--senpai-muted);
  padding: 2rem;
}
</style>
