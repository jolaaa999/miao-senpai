<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { consoleApi, type FeatureSwitch } from '@/api/client'
import { describeConfig } from '@/config/descriptions'

const items = ref<FeatureSwitch[]>([])
const filter = ref('')
const loading = ref(true)

const grouped = computed(() => {
  const q = filter.value.trim().toLowerCase()
  const filtered = q
    ? items.value.filter((f) => {
        const desc = describeConfig(f.key, f.description)
        return (
          f.key.toLowerCase().includes(q) ||
          f.value.toLowerCase().includes(q) ||
          desc.toLowerCase().includes(q)
        )
      })
    : items.value

  const map = new Map<string, FeatureSwitch[]>()
  for (const f of filtered) {
    const list = map.get(f.group) ?? []
    list.push(f)
    map.set(f.group, list)
  }
  return [...map.entries()].sort((a, b) => a[0].localeCompare(b[0], 'zh'))
})

function isEnabled(val: string) {
  return val === 'true' || val === '1'
}

onMounted(async () => {
  try {
    const res = await consoleApi.features()
    items.value = res.data.items.sort((a, b) => a.key.localeCompare(b.key))
  } finally {
    loading.value = false
  }
})
</script>

<template>
  <div v-loading="loading">
    <h1 class="page-title">功能开关</h1>
    <p class="page-subtitle">只读展示 .env 中的 DL_SENPAI / LLM 配置（敏感项已脱敏）</p>

    <div class="toolbar glass-card">
      <el-input v-model="filter" placeholder="搜索配置项…" clearable style="max-width: 320px" prefix-icon="Search" />
      <el-tag type="info">{{ items.length }} 项</el-tag>
    </div>

    <div v-for="[group, list] in grouped" :key="group" class="group-section glass-card">
      <h2>{{ group }}</h2>
      <div class="feature-header">
        <span>配置项</span>
        <span>当前值</span>
        <span>说明</span>
      </div>
      <div class="feature-list">
        <div v-for="f in list" :key="f.key" class="feature-row">
          <div class="feature-key">
            <code>{{ f.key }}</code>
            <el-tag v-if="isEnabled(f.value)" size="small" type="success">ON</el-tag>
            <el-tag v-else-if="f.value === 'false' || f.value === '0'" size="small" type="info">OFF</el-tag>
            <el-tag v-else-if="f.value.includes('未写入 .env')" size="small">默认</el-tag>
            <el-tag v-if="f.masked" size="small" type="warning">脱敏</el-tag>
          </div>
          <div class="feature-value">{{ f.value || '（空）' }}</div>
          <div class="feature-desc">{{ describeConfig(f.key, f.description) }}</div>
        </div>
      </div>
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
}

.group-section {
  padding: 1.25rem;
  margin-bottom: 1rem;

  h2 {
    margin: 0 0 1rem;
    font-size: 1rem;
    color: var(--senpai-accent-deep);
    font-weight: 600;
  }
}

.feature-header {
  display: grid;
  grid-template-columns: minmax(200px, 1.2fr) minmax(120px, 0.8fr) minmax(200px, 1.5fr);
  gap: 1rem;
  padding: 0 0 0.5rem;
  margin-bottom: 0.25rem;
  border-bottom: 1px solid var(--senpai-border);
  font-size: 0.75rem;
  font-weight: 600;
  color: var(--senpai-muted);
  text-transform: uppercase;
  letter-spacing: 0.04em;

  @media (max-width: 900px) {
    display: none;
  }
}

.feature-row {
  display: grid;
  grid-template-columns: minmax(200px, 1.2fr) minmax(120px, 0.8fr) minmax(200px, 1.5fr);
  gap: 1rem;
  padding: 0.65rem 0;
  border-bottom: 1px solid var(--senpai-border);
  font-size: 0.85rem;
  align-items: start;

  &:last-child {
    border-bottom: none;
  }

  @media (max-width: 900px) {
    grid-template-columns: 1fr;
    gap: 0.35rem;
  }
}

.feature-key {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;

  code {
    font-family: 'DM Sans', monospace;
    font-size: 0.8rem;
    color: var(--senpai-accent-deep);
  }
}

.feature-value {
  color: var(--senpai-muted);
  word-break: break-all;
  font-family: monospace;
  font-size: 0.8rem;
}

.feature-desc {
  color: var(--senpai-text);
  opacity: 0.85;
  font-size: 0.82rem;
  line-height: 1.5;
}
</style>
