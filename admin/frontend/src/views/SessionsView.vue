<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { consoleApi, type ChatTurn, type SessionSummary } from '@/api/client'
import ChatBubble from '@/components/ChatBubble.vue'
import { ElMessage, ElMessageBox } from 'element-plus'

const route = useRoute()
const sessions = ref<SessionSummary[]>([])
const selectedId = ref('')
const turns = ref<ChatTurn[]>([])
const loadingList = ref(true)
const loadingTurns = ref(false)
const filter = ref('')

const filtered = computed(() => {
  const q = filter.value.trim().toLowerCase()
  if (!q) return sessions.value
  return sessions.value.filter(
    (s) =>
      s.session_id.toLowerCase().includes(q) ||
      s.ref_id.includes(q) ||
      s.preview.toLowerCase().includes(q),
  )
})

async function loadSessions() {
  loadingList.value = true
  try {
    const res = await consoleApi.sessions()
    sessions.value = res.data.items.sort(
      (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime(),
    )
  } finally {
    loadingList.value = false
  }
}

async function selectSession(sid: string) {
  selectedId.value = sid
  loadingTurns.value = true
  try {
    const res = await consoleApi.turns(sid)
    turns.value = res.data.turns
  } finally {
    loadingTurns.value = false
  }
}

async function clearSession() {
  if (!selectedId.value) return
  await ElMessageBox.confirm('确定清空该 session 的聊天记忆？此操作不可恢复。', '清空记忆', {
    type: 'warning',
    confirmButtonText: '清空',
    cancelButtonText: '取消',
  })
  await consoleApi.clearSession(selectedId.value)
  turns.value = []
  ElMessage.success('已清空')
  await loadSessions()
}

function sessionLabel(s: SessionSummary) {
  return s.session_type === 'group' ? `群 ${s.ref_id}` : `私聊 ${s.ref_id}`
}

function formatTime(iso: string) {
  return new Date(iso).toLocaleString('zh-CN')
}

onMounted(async () => {
  await loadSessions()
  const q = route.query.sid as string
  if (q) await selectSession(q)
  else if (sessions.value.length) await selectSession(sessions.value[0].session_id)
})

watch(
  () => route.query.sid,
  (sid) => {
    if (typeof sid === 'string' && sid) selectSession(sid)
  },
)
</script>

<template>
  <div>
    <h1 class="page-title">聊天记录</h1>
    <p class="page-subtitle">
      浏览与管理学姐的对话记忆（每个群/私聊一个 session，默认无条数上限）。
      若列表为空，请先<strong>重启 bot</strong>后再 @ 学姐聊几句——旧版 Windows 路径问题可能导致历史记忆未正确写入。
    </p>

    <div class="chat-layout">
      <aside class="session-panel glass-card">
        <el-input v-model="filter" placeholder="搜索 session / 预览" clearable class="search" />
        <div v-loading="loadingList" class="session-list">
          <div
            v-for="s in filtered"
            :key="s.session_id"
            class="session-row"
            :class="{ active: s.session_id === selectedId }"
            @click="selectSession(s.session_id)"
          >
            <div class="row-top">
              <el-tag size="small" :type="s.session_type === 'group' ? 'warning' : 'success'">
                {{ sessionLabel(s) }}
              </el-tag>
              <span class="count">{{ s.message_count }} 条</span>
            </div>
            <div class="row-preview">{{ s.preview || '（空）' }}</div>
            <div class="row-time">{{ formatTime(s.updated_at) }}</div>
          </div>
          <div v-if="!filtered.length && !loadingList" class="empty">
            暂无 session。重启 bot 后 @ 学姐或私聊几轮，这里会出现「群 群号」或「私聊 QQ号」。
          </div>
        </div>
      </aside>

      <section class="chat-panel glass-card" v-loading="loadingTurns">
        <div v-if="selectedId" class="chat-header">
          <div>
            <strong>{{ selectedId }}</strong>
            <span class="turn-hint">{{ turns.length }} 条消息</span>
          </div>
          <el-button type="danger" plain size="small" @click="clearSession">清空记忆</el-button>
        </div>
        <div class="chat-scroll">
          <ChatBubble v-for="(t, i) in turns" :key="i" :role="t.role" :content="t.content" />
          <div v-if="!turns.length && selectedId" class="empty-chat">这个 session 还没有对话记录</div>
          <div v-if="!selectedId" class="empty-chat">← 选择一个 session</div>
        </div>
      </section>
    </div>
  </div>
</template>

<style scoped lang="scss">
.chat-layout {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 1rem;
  height: calc(100vh - 140px);
  min-height: 480px;

  @media (max-width: 800px) {
    grid-template-columns: 1fr;
    height: auto;
  }
}

.session-panel {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.search {
  margin: 1rem 1rem 0.5rem;
}

.session-list {
  flex: 1;
  overflow-y: auto;
  padding: 0 0.5rem 0.5rem;
}

.session-row {
  padding: 0.75rem;
  border-radius: 10px;
  cursor: pointer;
  margin-bottom: 0.25rem;
  border: 1px solid transparent;
  transition: all 0.2s;

  &:hover {
    background: var(--senpai-surface-hover);
  }

  &.active {
    background: var(--senpai-accent-soft);
    border-color: var(--senpai-border);
  }
}

.row-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 0.35rem;

  .count {
    font-size: 0.7rem;
    color: var(--senpai-muted);
  }
}

.row-preview {
  font-size: 0.8rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.row-time {
  font-size: 0.7rem;
  color: var(--senpai-muted);
  margin-top: 0.25rem;
}

.chat-panel {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.chat-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 1rem 1.25rem;
  border-bottom: 1px solid var(--senpai-border);

  strong {
    font-size: 0.9rem;
  }

  .turn-hint {
    margin-left: 0.75rem;
    font-size: 0.8rem;
    color: var(--senpai-muted);
    font-weight: normal;
  }
}

.chat-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 1.25rem;
}

.empty,
.empty-chat {
  text-align: center;
  color: var(--senpai-muted);
  padding: 2rem;
  font-size: 0.9rem;
}
</style>
