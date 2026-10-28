<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { consoleApi } from '@/api/client'

interface TrendItem {
  source: string
  title: string
  rank: number
  hot: string
}

const items = ref<TrendItem[]>([])
const fetchedAt = ref('')
const loading = ref(false)
const sourceFilter = ref('')

// 热榜接口一次给多个平台，先把来源列出来再让用户挑
const sources = computed(() => [...new Set(items.value.map((it) => it.source))].sort())

const visible = computed(() => {
  const list = sourceFilter.value ? items.value.filter((it) => it.source === sourceFilter.value) : items.value
  return [...list].sort((a, b) => a.source.localeCompare(b.source) || a.rank - b.rank)
})

function sourceLabel(source: string) {
  const map: Record<string, string> = {
    weibo: '微博',
    douyin: '抖音',
    bilibili: 'B站',
    zhihu: '知乎',
    baidu: '百度',
    toutiao: '头条',
  }
  return map[source] ?? source
}

function hotLabel(hot: string) {
  const n = Number(hot)
  if (!hot || Number.isNaN(n)) return hot || '—'
  if (n >= 10000) return `${(n / 10000).toFixed(1)} 万`
  return String(n)
}

async function load() {
  loading.value = true
  try {
    const res = await consoleApi.trends()
    items.value = res.data.items ?? []
    // 后端给的是 unix 秒
    const ts = Number(res.data.fetched_at)
    fetchedAt.value = ts ? new Date(ts * 1000).toLocaleString('zh-CN') : '—'
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<template>
  <div>
    <h1 class="page-title">热梗缓存</h1>
    <p class="page-subtitle">
      学姐聊天时会参考近期热榜标题接梗。这里是她手里那份缓存：抓取时间 {{ fetchedAt }}，
      共 {{ items.length }} 条。缓存由 bot 侧定时刷新，本页只读。
    </p>

    <div class="toolbar glass-card">
      <span>来源：</span>
      <el-select v-model="sourceFilter" placeholder="全部来源" clearable style="width: 180px">
        <el-option v-for="s in sources" :key="s" :label="sourceLabel(s)" :value="s" />
      </el-select>
      <el-button @click="load">刷新</el-button>
    </div>

    <el-table v-loading="loading" :data="visible" size="small" class="glass-card trend-table">
      <el-table-column label="来源" width="100">
        <template #default="{ row }">
          <el-tag size="small" type="warning" effect="plain">{{ sourceLabel(row.source) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="rank" label="#" width="60" />
      <el-table-column prop="title" label="标题" />
      <el-table-column label="热度" width="110">
        <template #default="{ row }">{{ hotLabel(row.hot) }}</template>
      </el-table-column>
    </el-table>
  </div>
</template>

<style scoped lang="scss">
.toolbar {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 1rem 1.25rem;
  margin-bottom: 1rem;

  span {
    color: var(--senpai-muted);
  }
}

.trend-table {
  padding: 0.5rem;
}
</style>
