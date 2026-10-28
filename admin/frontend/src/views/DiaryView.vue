<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { consoleApi, type DiarySnapshot } from '@/api/client'

const snap = ref<DiarySnapshot>({ items: [], days: [], total: 0, path: '' })
const loading = ref(false)
const activeDay = ref('') // 空 = 全部
const activeTag = ref('')

// 标签是全量统计出来的，点一下就能只看来自身某个平台的内容
const tagOptions = computed(() => {
  const counter = new Map<string, number>()
  for (const item of snap.value.items) {
    for (const tag of item.tags ?? []) {
      counter.set(tag, (counter.get(tag) ?? 0) + 1)
    }
  }
  return [...counter.entries()].sort((a, b) => b[1] - a[1])
})

const visibleItems = computed(() =>
  snap.value.items.filter((item) => {
    if (activeDay.value && item.day !== activeDay.value) return false
    if (activeTag.value && !(item.tags ?? []).includes(activeTag.value)) return false
    return true
  }),
)

async function load() {
  loading.value = true
  try {
    const res = await consoleApi.diary(500)
    snap.value = res.data
  } finally {
    loading.value = false
  }
}

function tierType(tier: string) {
  if (tier === '重要') return 'danger'
  if (tier === '特别') return 'warning'
  return 'info'
}

onMounted(load)
</script>

<template>
  <div>
    <h1 class="page-title">学姐日记</h1>
    <p class="page-subtitle">
      学姐「自己过日子」时写下的短记：刷到视频、看到什么东西后的反应，一天攒若干条。
      数据源 <code>data/dl_senpai/diary/index.jsonl</code>，共 {{ snap.total }} 条。
    </p>

    <div class="toolbar glass-card">
      <div class="day-filter">
        <el-tag
          size="small"
          :effect="activeDay === '' ? 'dark' : 'plain'"
          class="clickable"
          @click="activeDay = ''"
        >
          全部 {{ snap.total }}
        </el-tag>
        <el-tag
          v-for="d in snap.days"
          :key="d.day"
          size="small"
          :effect="activeDay === d.day ? 'dark' : 'plain'"
          class="clickable"
          @click="activeDay = d.day === activeDay ? '' : d.day"
        >
          {{ d.day }}（{{ d.count }}）
        </el-tag>
      </div>
      <div class="tag-filter">
        <span class="label">标签</span>
        <el-tag
          v-for="[tag, count] in tagOptions"
          :key="tag"
          size="small"
          type="warning"
          :effect="activeTag === tag ? 'dark' : 'plain'"
          class="clickable"
          @click="activeTag = activeTag === tag ? '' : tag"
        >
          {{ tag }} {{ count }}
        </el-tag>
        <el-button size="small" @click="load">刷新</el-button>
      </div>
    </div>

    <div v-loading="loading" class="diary-list">
      <div v-for="item in visibleItems" :key="item.id" class="diary-card glass-card">
        <div class="diary-head">
          <span class="title">{{ item.title || '（无标题）' }}</span>
          <el-tag v-if="item.tier" size="small" :type="tierType(item.tier)">{{ item.tier }}</el-tag>
          <el-tag v-for="tag in item.tags" :key="tag" size="small" type="warning" effect="plain">{{ tag }}</el-tag>
        </div>
        <p class="text">{{ item.text }}</p>
        <div v-if="item.mood || item.angle" class="extra">
          <span v-if="item.mood">心情：{{ item.mood }}</span>
          <span v-if="item.angle">视角：{{ item.angle }}</span>
        </div>
        <div class="meta">
          <span>{{ item.created_at || item.day }}</span>
          <span class="id-hint">#{{ item.id }}</span>
        </div>
      </div>
      <div v-if="!visibleItems.length && !loading" class="empty glass-card">这个筛选下没有日记</div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.toolbar {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
  padding: 0.9rem 1.1rem;
  margin-bottom: 1rem;
}

.day-filter,
.tag-filter {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.4rem;
}

.tag-filter .label {
  font-size: 0.75rem;
  color: var(--senpai-muted);
  margin-right: 0.2rem;
}

.clickable {
  cursor: pointer;
  transition: transform 0.15s;

  &:hover {
    transform: translateY(-1px);
  }
}

.diary-list {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
  min-height: 120px;
}

.diary-card {
  padding: 0.85rem 1.1rem 0.9rem;
}

.diary-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin-bottom: 0.45rem;

  .title {
    font-weight: 600;
    font-size: 0.92rem;
    color: var(--senpai-text);
    margin-right: 0.2rem;
  }
}

.text {
  margin: 0 0 0.45rem;
  font-size: 0.86rem;
  line-height: 1.6;
  color: var(--senpai-text);
  white-space: pre-wrap;
}

.extra {
  display: flex;
  gap: 1rem;
  font-size: 0.76rem;
  color: var(--senpai-muted);
  margin-bottom: 0.35rem;
}

.meta {
  display: flex;
  justify-content: space-between;
  font-size: 0.72rem;
  color: var(--senpai-muted);
  font-variant-numeric: tabular-nums;
}

.id-hint {
  opacity: 0.7;
}

.empty {
  text-align: center;
  padding: 2.5rem;
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
