<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { consoleApi, type EditableFeature, type FeatureSwitch } from '@/api/client'
import { describeConfig } from '@/config/descriptions'

const items = ref<FeatureSwitch[]>([])
const editable = ref<EditableFeature[]>([])
const filter = ref('')
const loading = ref(true)
const savingKey = ref('')
const batchBusy = ref(false)

/** 可写开关的 key -> 当前值，供开关控件读取与回写。 */
const editableMap = computed(() => {
  const m = new Map<string, EditableFeature>()
  for (const e of editable.value) m.set(e.key, e)
  return m
})

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

/** 该键是否可写（非凭据、布尔型、前缀允许）。 */
function writable(f: FeatureSwitch): boolean {
  return editableMap.value.has(f.key)
}

async function load() {
  loading.value = true
  try {
    const [all, ed] = await Promise.all([
      consoleApi.features(),
      consoleApi.editableFeatures(),
    ])
    items.value = all.data.items.sort((a, b) => a.key.localeCompare(b.key))
    editable.value = ed.data.items
  } finally {
    loading.value = false
  }
}

onMounted(load)

/**
 * 切换开关。
 *
 * 写入 .env / .env.dev，bot 侧 config_watcher 会在数秒内热重载 ——
 * 不需要重启，但也不是瞬时，因此提示语如实说明。
 */
async function toggle(f: FeatureSwitch, next: boolean) {
  const value = next ? 'true' : 'false'
  savingKey.value = f.key
  try {
    const res = await consoleApi.setFeature(f.key, value)
    // 同步本地展示，避免整页重载造成闪烁
    const target = items.value.find((x) => x.key === f.key)
    if (target) target.value = value
    const ed = editableMap.value.get(f.key)
    if (ed) ed.value = value
    const files = res.data.written_to?.join('、') ?? ''
    ElMessage.success(`已写入 ${files}，bot 将在数秒内生效`)
  } catch (e: unknown) {
    const msg =
      (e as { response?: { data?: { error?: string } } })?.response?.data?.error ??
      '写入失败'
    ElMessage.error(msg)
  } finally {
    savingKey.value = ''
  }
}

/**
 * 非对话功能清单：一键全部关闭，只保留核心对话与识图。
 *
 * 与用户约定一致：vision（识图）属于对话能力，不在关闭范围内。
 * 二次确认是必要的 —— 这是批量写盘，误点影响面较大。
 */
const NON_CHAT_KEYS = [
  'DL_SENPAI_STICKER_ENABLE',
  'DL_SENPAI_CARD_ENABLE',
  'DL_SENPAI_MUTE_ENABLE',
  'DL_SENPAI_VOICE_ENABLE',
  'DL_SENPAI_TRENDS_ENABLE',
  'DL_SENPAI_SEARCH_ENABLE',
  'DL_SENPAI_DRAW_ENABLE',
  'DL_SENPAI_BROWSER_ENABLE',
  'DL_SENPAI_WELCOME_ENABLE',
  'DL_SENPAI_VERIFY_ENABLE',
  'DL_SENPAI_CHECKIN_ENABLE',
  'DL_SENPAI_AFFECTION_ENABLE',
  'DL_SENPAI_SHOP_ENABLE',
  'DL_SENPAI_LIKE_ENABLE',
  'DL_SENPAI_QZONE_ENABLE',
  'DL_SENPAI_PERSON_MEMORY_ENABLE',
  'DL_SENPAI_MEMORY_RECALL_ENABLE',
  'DL_SENPAI_INNER_STATE_ENABLE',
  'DL_SENPAI_GROUP_STYLE_ENABLE',
  'DL_SENPAI_FEWSHOT_ENABLE',
]

async function disableNonChat() {
  const targets = items.value.filter(
    (f) => NON_CHAT_KEYS.includes(f.key) && writable(f) && isEnabled(f.value),
  )
  if (!targets.length) {
    ElMessage.info('没有需要关闭的功能（都已关闭或不可写）')
    return
  }
  try {
    await ElMessageBox.confirm(
      `将关闭 ${targets.length} 个非对话功能，只保留核心对话与识图。`,
      '确认批量关闭',
      { type: 'warning', confirmButtonText: '确认关闭', cancelButtonText: '取消' },
    )
  } catch {
    return
  }

  batchBusy.value = true
  let ok = 0
  const failed: string[] = []
  for (const f of targets) {
    try {
      await consoleApi.setFeature(f.key, 'false')
      f.value = 'false'
      const ed = editableMap.value.get(f.key)
      if (ed) ed.value = 'false'
      ok += 1
    } catch {
      failed.push(f.key)
    }
  }
  batchBusy.value = false

  if (failed.length) {
    ElMessage.warning(`已关闭 ${ok} 项，失败 ${failed.length} 项：${failed.join(', ')}`)
  } else {
    ElMessage.success(`已关闭 ${ok} 项非对话功能，bot 将在数秒内生效`)
  }
}
</script>

<template>
  <div v-loading="loading">
    <h1 class="page-title">功能开关</h1>
    <p class="page-subtitle">
      布尔项可直接开关，写入 .env / .env.dev 后 bot 数秒内热重载生效，无需重启。
      凭据类（API Key / Token）与文本项保持只读。
    </p>

    <div class="toolbar glass-card">
      <el-input v-model="filter" placeholder="搜索配置项…" clearable style="max-width: 320px" prefix-icon="Search" />
      <el-tag type="info">{{ items.length }} 项</el-tag>
      <el-tag type="success">{{ editable.length }} 项可开关</el-tag>
      <el-button
        type="danger"
        plain
        :loading="batchBusy"
        @click="disableNonChat"
      >
        关闭全部非对话功能
      </el-button>
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
            <el-tag v-if="f.masked" size="small" type="warning">脱敏</el-tag>
            <el-tag v-else-if="!writable(f)" size="small">只读</el-tag>
            <el-tag v-else-if="f.value.includes('未写入')" size="small">默认</el-tag>
          </div>
          <div class="feature-value">
            <el-switch
              v-if="writable(f)"
              :model-value="isEnabled(f.value)"
              :loading="savingKey === f.key"
              :disabled="batchBusy"
              inline-prompt
              active-text="开"
              inactive-text="关"
              @update:model-value="(v: boolean | string | number) => toggle(f, Boolean(v))"
            />
            <span v-else class="readonly-value">{{ f.value || '（空）' }}</span>
          </div>
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
  font-size: 0.8rem;
  display: flex;
  align-items: center;
}

// 只读值仍用等宽字体，与可交互的开关在视觉上拉开区分
.readonly-value {
  font-family: monospace;
  opacity: 0.7;
}

.feature-desc {
  color: var(--senpai-text);
  opacity: 0.85;
  font-size: 0.82rem;
  line-height: 1.5;
}
</style>
