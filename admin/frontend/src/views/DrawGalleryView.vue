<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { consoleApi, type DrawItem } from '@/api/client'
import { ElMessage, ElMessageBox } from 'element-plus'

const sessions = ref<{ session_id: string; item_count: number }[]>([])
const selectedSession = ref('')
const items = ref<DrawItem[]>([])
const imageUrls = ref<Record<string, string>>({})
const loading = ref(false)

function sessionLabel(id: string) {
  if (id === '__all_files__') return '全部图片文件'
  if (id.startsWith('group:')) return `群 ${id.slice(6)}`
  if (id.startsWith('private:')) return `私聊 ${id.slice(8)}`
  return id
}

function sceneLabel(scene: string) {
  const map: Record<string, string> = {
    selfie: '自拍',
    general: '通用',
    outfit: '换装',
  }
  return map[scene] || scene || '—'
}

function filenameFromItem(item: DrawItem) {
  if (!item.local_path) return ''
  return item.local_path.split(/[/\\]/).pop() ?? ''
}

function formatTime(iso: string) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('zh-CN')
}

function requesterLabel(item: DrawItem) {
  const name = item.source_user?.trim()
  const id = item.source_user_id?.trim()
  if (name && id) return `${name}（${id}）`
  if (id) return id
  if (name) return name
  return '—'
}

function revokeImages() {
  for (const url of Object.values(imageUrls.value)) {
    URL.revokeObjectURL(url)
  }
  imageUrls.value = {}
}

async function loadItemImages() {
  revokeImages()
  for (const item of items.value) {
    const name = filenameFromItem(item)
    if (!name) continue
    try {
      const res = await consoleApi.drawAsset(name)
      imageUrls.value[item.id] = URL.createObjectURL(res.data)
    } catch {
      /* 占位 */
    }
  }
}

async function loadSessions() {
  const res = await consoleApi.drawSessions()
  sessions.value = res.data.items
  if (!selectedSession.value && sessions.value.length) {
    const preferred = sessions.value.find((s) => s.session_id !== '__all_files__')
    selectedSession.value = preferred?.session_id ?? sessions.value[0].session_id
  }
}

async function loadItems() {
  if (!selectedSession.value) return
  loading.value = true
  try {
    const res = await consoleApi.drawItems(selectedSession.value)
    items.value = res.data.items.sort((a, b) => {
      const ta = a.created_at || ''
      const tb = b.created_at || ''
      return tb.localeCompare(ta)
    })
    await loadItemImages()
  } finally {
    loading.value = false
  }
}

async function removeItem(item: DrawItem) {
  if (selectedSession.value === '__all_files__') {
    ElMessage.warning('「全部图片文件」视图暂不支持删除，请切换到具体 session')
    return
  }
  await ElMessageBox.confirm(`删除图片 ${item.id.slice(0, 8)}…？`, '删除', { type: 'warning' })
  await consoleApi.deleteDraw(selectedSession.value, item.id)
  ElMessage.success('已删除')
  await loadItems()
  await loadSessions()
}

onMounted(async () => {
  await loadSessions()
  await loadItems()
})

onUnmounted(revokeImages)

watch(selectedSession, loadItems)
</script>

<template>
  <div>
    <h1 class="page-title">生图库</h1>
    <p class="page-subtitle">
      学姐每次成功生图都会归档到本地（含提示词与 session），图片保存在
      <code>data/dl_senpai/draw/files/</code>。
      元数据在对应群的 JSON 索引里；「全部图片文件」会尝试合并索引，无记录时显示占位说明。
    </p>

    <div class="toolbar glass-card">
      <span>Session：</span>
      <el-select v-model="selectedSession" style="width: 320px">
        <el-option
          v-for="s in sessions"
          :key="s.session_id"
          :label="`${sessionLabel(s.session_id)}（${s.item_count}）`"
          :value="s.session_id"
        />
      </el-select>
      <el-button @click="loadItems">刷新</el-button>
    </div>

    <div v-loading="loading" class="draw-grid">
      <div v-for="item in items" :key="item.id" class="draw-card glass-card">
        <div class="draw-preview">
          <img v-if="imageUrls[item.id]" :src="imageUrls[item.id]" class="draw-img" alt="draw" />
          <div v-else class="placeholder">🖼️</div>
        </div>
        <div class="draw-body">
          <div class="tags">
            <el-tag v-if="item.scene" size="small" type="warning">{{ sceneLabel(item.scene) }}</el-tag>
            <el-tag v-if="item.model" size="small" type="info">{{ item.model }}</el-tag>
            <el-tag v-if="item.size" size="small">{{ item.size }}</el-tag>
          </div>
          <div class="info-block">
            <span class="label">请求账号</span>
            <span class="value account">{{ requesterLabel(item) }}</span>
          </div>
          <div v-if="item.user_request" class="info-block">
            <span class="label">用户原话</span>
            <p class="value">{{ item.user_request }}</p>
          </div>
          <div class="info-block prompt-block">
            <span class="label">生图提示词</span>
            <p class="value prompt">{{ item.prompt || '（无提示词）' }}</p>
          </div>
          <div class="meta">
            <span>{{ formatTime(item.created_at) }}</span>
            <span class="id-hint">{{ item.id.slice(0, 8) }}</span>
          </div>
          <el-button
            v-if="selectedSession !== '__all_files__'"
            size="small"
            type="danger"
            plain
            @click="removeItem(item)"
          >
            删除
          </el-button>
        </div>
      </div>
      <div v-if="!items.length && !loading" class="empty glass-card">该 session 暂无生图记录</div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.toolbar {
  display: flex;
  align-items: center;
  gap: 1rem;
  padding: 1rem 1.25rem;
  margin-bottom: 1rem;

  span {
    color: var(--senpai-muted);
  }
}

.draw-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 1rem;
  min-height: 200px;
}

.draw-card {
  overflow: hidden;
  transition: transform 0.2s;

  &:hover {
    transform: translateY(-2px);
  }
}

.draw-preview {
  height: 200px;
  background: linear-gradient(135deg, rgba(251, 146, 60, 0.12), rgba(56, 189, 248, 0.1));
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

.draw-img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}

.placeholder {
  font-size: 2.5rem;
  opacity: 0.5;
}

.draw-body {
  padding: 0.75rem 1rem 1rem;
}

.tags {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
  margin-bottom: 0.65rem;
}

.info-block {
  margin-bottom: 0.55rem;

  .label {
    display: block;
    font-size: 0.68rem;
    font-weight: 600;
    color: var(--senpai-accent-deep);
    margin-bottom: 0.2rem;
    letter-spacing: 0.02em;
  }

  .value {
    font-size: 0.82rem;
    color: var(--senpai-text);
    line-height: 1.45;
    margin: 0;
  }

  .account {
    font-weight: 500;
  }
}

.prompt-block .prompt {
  font-size: 0.8rem;
  color: var(--senpai-muted);
  max-height: 120px;
  overflow-y: auto;
  white-space: pre-wrap;
  word-break: break-word;
  background: rgba(255, 255, 255, 0.45);
  padding: 0.45rem 0.55rem;
  border-radius: 6px;
  border: 1px solid rgba(251, 146, 60, 0.15);
}

.meta {
  display: flex;
  justify-content: space-between;
  font-size: 0.7rem;
  color: var(--senpai-muted);
  margin-bottom: 0.5rem;
  margin-top: 0.25rem;
}

.id-hint {
  opacity: 0.7;
}

.empty {
  grid-column: 1 / -1;
  text-align: center;
  padding: 3rem;
  color: var(--senpai-muted);
}

code {
  font-size: 0.85em;
  color: var(--senpai-accent-deep);
  background: var(--senpai-accent-soft);
  padding: 0.1rem 0.35rem;
  border-radius: 4px;
}
</style>
