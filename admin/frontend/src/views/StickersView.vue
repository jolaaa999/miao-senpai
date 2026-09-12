<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { consoleApi, type StickerItem } from '@/api/client'
import { ElMessage, ElMessageBox } from 'element-plus'

const sessions = ref<{ session_id: string; item_count: number }[]>([])
const selectedSession = ref('')
const items = ref<StickerItem[]>([])
const imageUrls = ref<Record<string, string>>({})
const loading = ref(false)

function sessionLabel(id: string) {
  if (id === '__all_files__') return '全部表情文件'
  return id
}

function filenameFromItem(item: StickerItem) {
  if (!item.local_path) return ''
  return item.local_path.split(/[/\\]/).pop() ?? ''
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
      const res = await consoleApi.stickerAsset(name)
      imageUrls.value[item.id] = URL.createObjectURL(res.data)
    } catch {
      /* 图片加载失败则显示占位 */
    }
  }
}

async function loadSessions() {
  const res = await consoleApi.stickerSessions()
  sessions.value = res.data.items
  if (!selectedSession.value && sessions.value.length) {
    selectedSession.value = sessions.value[0].session_id
  }
}

async function loadItems() {
  if (!selectedSession.value) return
  loading.value = true
  try {
    const res = await consoleApi.stickerItems(selectedSession.value)
    items.value = res.data.items.sort((a, b) => b.use_count - a.use_count)
    await loadItemImages()
  } finally {
    loading.value = false
  }
}

async function removeItem(item: StickerItem) {
  if (selectedSession.value === '__all_files__') {
    ElMessage.warning('「全部表情文件」视图暂不支持删除，请切换到具体 session')
    return
  }
  await ElMessageBox.confirm(`删除表情 ${item.id.slice(0, 8)}…？`, '删除', { type: 'warning' })
  await consoleApi.deleteSticker(selectedSession.value, item.id)
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
    <h1 class="page-title">表情库</h1>
    <p class="page-subtitle">
      学姐被动收集的表情包。若 session 列表为空但「全部表情文件」有内容，说明历史索引因 Windows 路径问题损坏，新收集的表情会正常显示。
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

    <div v-loading="loading" class="sticker-grid">
      <div v-for="item in items" :key="item.id" class="sticker-card glass-card">
        <div class="sticker-preview">
          <span v-if="item.has_local_file" class="badge">本地</span>
          <span v-else class="badge remote">远程</span>
          <img v-if="imageUrls[item.id]" :src="imageUrls[item.id]" class="sticker-img" alt="sticker" />
          <div v-else class="emoji-placeholder">🖼️</div>
        </div>
        <div class="sticker-body">
          <div v-if="item.keywords?.length" class="keywords">
            <el-tag v-for="kw in item.keywords.slice(0, 4)" :key="kw" size="small" type="info">
              {{ kw }}
            </el-tag>
          </div>
          <p class="context">{{ item.context_text || '（无上下文）' }}</p>
          <div class="meta">
            <span>使用 {{ item.use_count }} 次</span>
            <span>{{ item.source_user || item.id.slice(0, 8) }}</span>
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
      <div v-if="!items.length && !loading" class="empty glass-card">该 session 暂无表情</div>
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

.sticker-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: 1rem;
  min-height: 200px;
}

.sticker-card {
  overflow: hidden;
  transition: transform 0.2s;

  &:hover {
    transform: translateY(-2px);
  }
}

.sticker-preview {
  position: relative;
  height: 140px;
  background: linear-gradient(135deg, rgba(251, 146, 60, 0.15), rgba(56, 189, 248, 0.12));
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
}

.sticker-img {
  max-width: 100%;
  max-height: 100%;
  object-fit: contain;
}

.emoji-placeholder {
  font-size: 2.5rem;
  opacity: 0.6;
}

.badge {
  position: absolute;
  top: 0.5rem;
  right: 0.5rem;
  z-index: 1;
  font-size: 0.65rem;
  padding: 0.15rem 0.5rem;
  border-radius: 999px;
  background: var(--senpai-success);
  color: #064e3b;

  &.remote {
    background: var(--senpai-warning);
    color: #78350f;
  }
}

.sticker-body {
  padding: 0.75rem 1rem 1rem;
}

.keywords {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
  margin-bottom: 0.5rem;
}

.context {
  font-size: 0.8rem;
  color: var(--senpai-muted);
  margin: 0 0 0.5rem;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.meta {
  display: flex;
  justify-content: space-between;
  font-size: 0.7rem;
  color: var(--senpai-muted);
  margin-bottom: 0.5rem;
}

.empty {
  grid-column: 1 / -1;
  text-align: center;
  padding: 3rem;
  color: var(--senpai-muted);
}
</style>
