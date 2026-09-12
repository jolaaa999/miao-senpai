<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  consoleApi,
  type FeatureSwitch,
  type GroupFeatureCatalogItem,
  type GroupFeatureRow,
} from '@/api/client'
import { ElMessage } from 'element-plus'
import { RefreshRight } from '@element-plus/icons-vue'

const rows = ref<GroupFeatureRow[]>([])
const catalog = ref<GroupFeatureCatalogItem[]>([])
const globalSwitches = ref<FeatureSwitch[]>([])
const loading = ref(true)
const filter = ref('')
const savingKeys = ref(new Set<string>())

// —— 全局 .env 开关（跟随全局时的显示基准）——
// 与 config.py 默认值对齐：绝大多数功能默认开；主动社交默认关防风控
const DEFAULT_OFF_KEYS = new Set(['friend_add', 'like', 'qzone'])

function envBool(featureKey: string): boolean {
  const envKey = `DL_SENPAI_${featureKey.toUpperCase()}_ENABLE`
  const item = globalSwitches.value.find((f) => f.key === envKey)
  const fallback = !DEFAULT_OFF_KEYS.has(featureKey)
  if (!item) return fallback
  const v = item.value.trim().toLowerCase()
  // 控制台对未写入 .env 的项会显示「（默认，未写入 .env）」
  if (!v || v.includes('默认')) return fallback
  return v === 'true' || v === '1'
}

const globalMap = ref<Record<string, boolean>>({})
const checkinGroupsGlobal = ref<string[]>([])

function computeGlobals() {
  const map: Record<string, boolean> = {}
  for (const f of catalog.value) {
    map[f.key] = envBool(f.key)
  }
  globalMap.value = map
  const rawItem = globalSwitches.value.find((f) => f.key === 'DL_SENPAI_CHECKIN_GROUPS')
  const raw = rawItem?.value ?? ''
  checkinGroupsGlobal.value = raw
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
}

function globalEnabled(key: string, gid: string): boolean {
  const on = globalMap.value[key] ?? true
  if (key === 'checkin') {
    // 签到全局语义 = 总开关 + 白名单群
    if (checkinGroupsGlobal.value.length === 0) return false
    return on && checkinGroupsGlobal.value.includes(gid)
  }
  return on
}

function effective(row: GroupFeatureRow, key: string): boolean {
  const override = row.features?.[key]
  if (typeof override === 'boolean') return override
  return globalEnabled(key, row.group_id)
}

function isOverride(row: GroupFeatureRow, key: string): boolean {
  return typeof row.features?.[key] === 'boolean'
}

// —— 数据加载 ——

async function load() {
  loading.value = true
  try {
    const [gfRes, fsRes] = await Promise.all([
      consoleApi.groupFeatures(),
      consoleApi.features().catch(() => ({ data: { items: [] as FeatureSwitch[] } })),
    ])
    rows.value = gfRes.data.items
    catalog.value = gfRes.data.features
    globalSwitches.value = fsRes.data.items
    computeGlobals()
  } finally {
    loading.value = false
  }
}

// —— 单个开关 ——

const keyOf = (gid: string, key: string) => `${gid}:${key}`
const saving = (gid: string, key: string) => savingKeys.value.has(keyOf(gid, key))

async function onToggle(row: GroupFeatureRow, key: string, value: boolean) {
  const k = keyOf(row.group_id, key)
  savingKeys.value.add(k)
  try {
    const res = await consoleApi.patchGroupFeature(row.group_id, key, value)
    row.features = res.data.features
    row.updated_at = res.data.updated_at
  } catch {
    ElMessage.error('保存失败，请重试')
  } finally {
    savingKeys.value.delete(k)
  }
}

async function onReset(row: GroupFeatureRow, key: string) {
  const k = keyOf(row.group_id, key)
  savingKeys.value.add(k)
  try {
    const res = await consoleApi.patchGroupFeature(row.group_id, key, null)
    row.features = res.data.features
    row.updated_at = res.data.updated_at
    ElMessage.success('已恢复跟随全局')
  } catch {
    ElMessage.error('保存失败，请重试')
  } finally {
    savingKeys.value.delete(k)
  }
}

// —— 列头批量操作 ——

async function batchSet(key: string, value: boolean | null) {
  const targets = filteredRows.value
  if (!targets.length) return
  loading.value = true
  try {
    await Promise.all(
      targets.map((row) =>
        consoleApi
          .patchGroupFeature(row.group_id, key, value)
          .then((res) => {
            row.features = res.data.features
            row.updated_at = res.data.updated_at
          })
          .catch(() => undefined),
      ),
    )
    ElMessage.success(value === null ? '已全部恢复跟随全局' : value ? '已全部开启' : '已全部关闭')
  } finally {
    loading.value = false
  }
}

function onColCommand(key: string, cmd: string | number | object) {
  if (cmd === 'on') batchSet(key, true)
  else if (cmd === 'off') batchSet(key, false)
  else if (cmd === 'reset') batchSet(key, null)
}

function onSwitchChange(row: GroupFeatureRow, key: string, val: string | number | boolean) {
  onToggle(row, key, !!val)
}

// —— 过滤 ——

const filteredRows = computed(() => {
  const q = filter.value.trim().toLowerCase()
  if (!q) return rows.value
  return rows.value.filter(
    (r) => r.group_id.includes(q) || (r.name || '').toLowerCase().includes(q),
  )
})

const overrideCount = computed(
  () =>
    rows.value.reduce(
      (acc, r) => acc + Object.values(r.features ?? {}).filter((v) => typeof v === 'boolean').length,
      0,
    ),
)

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <h1 class="page-title">群功能开关</h1>
    <p class="page-subtitle">
      每个群可单独覆盖学姐的功能开关；默认跟随「功能开关」页的全局配置，bot 侧改动即时生效、无需重启
    </p>

    <div class="toolbar glass-card">
      <el-input
        v-model="filter"
        placeholder="搜索群号 / 群名…"
        clearable
        style="max-width: 280px"
        prefix-icon="Search"
      />
      <el-button @click="load" :icon="RefreshRight">刷新</el-button>
      <el-tag type="info">{{ filteredRows.length }} 个群</el-tag>
      <el-tag v-if="overrideCount" type="warning">{{ overrideCount }} 项群级覆盖</el-tag>
      <span class="legend">
        <el-switch size="small" model-value />
        <span>实色 = 本群覆盖</span>
        <el-switch size="small" model-value class="muted-switch" />
        <span>浅色 = 跟随全局</span>
      </span>
    </div>

    <div class="table-wrap glass-card">
      <el-empty v-if="!loading && rows.length === 0" description="暂无群数据 — bot 上线后会自动同步群列表" />
      <el-table
        v-else
        :data="filteredRows"
        stripe
        style="width: 100%"
      >
        <el-table-column label="群" min-width="180" fixed="left">
          <template #default="{ row }">
            <div class="group-cell">
              <div class="group-id">{{ row.group_id }}</div>
              <div class="group-name">{{ row.name || '（未知群名）' }}</div>
            </div>
          </template>
        </el-table-column>

        <el-table-column
          v-for="f in catalog"
          :key="f.key"
          :label="f.label"
          align="center"
          min-width="92"
        >
          <template #header>
            <el-dropdown
              trigger="click"
              @command="(cmd: string | number | object) => onColCommand(f.key, cmd)"
            >
              <span class="col-header">
                {{ f.label }}
                <el-tag
                  size="small"
                  :type="globalMap[f.key] ? 'success' : 'info'"
                  class="global-tag"
                >
                  {{ globalMap[f.key] ? '全局开' : '全局关' }}
                </el-tag>
              </span>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="on">本列全部开启</el-dropdown-item>
                  <el-dropdown-item command="off">本列全部关闭</el-dropdown-item>
                  <el-dropdown-item command="reset">本列恢复跟随全局</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
          </template>
          <template #default="{ row }">
            <div class="switch-cell">
              <el-tooltip
                :content="
                  isOverride(row, f.key)
                    ? '本群覆盖'
                    : `跟随全局（当前${globalEnabled(f.key, row.group_id) ? '开' : '关'}）`
                "
                placement="top"
              >
                <el-switch
                  :model-value="effective(row, f.key)"
                  :class="{ 'muted-switch': !isOverride(row, f.key) }"
                  :loading="saving(row.group_id, f.key)"
                  @change="(val: string | number | boolean) => onSwitchChange(row, f.key, val)"
                />
              </el-tooltip>
              <el-icon
                v-if="isOverride(row, f.key)"
                class="reset-btn"
                title="恢复跟随全局"
                @click="onReset(row, f.key)"
              >
                <RefreshRight />
              </el-icon>
            </div>
          </template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<style scoped lang="scss">
.toolbar {
  display: flex;
  align-items: center;
  gap: 1rem;
  flex-wrap: wrap;
  padding: 1rem 1.25rem;
  margin-bottom: 1rem;
}

.legend {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  margin-left: auto;
  color: var(--senpai-muted);
  font-size: 0.78rem;

  .muted-switch {
    opacity: 0.5;
  }
}

.table-wrap {
  padding: 1rem;
  overflow-x: auto;
}

.group-cell {
  line-height: 1.3;

  .group-id {
    font-weight: 700;
    font-family: 'DM Sans', monospace;
    color: var(--senpai-accent-deep);
  }

  .group-name {
    font-size: 0.78rem;
    color: var(--senpai-muted);
  }
}

.col-header {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  white-space: nowrap;
  font-weight: 600;

  .global-tag {
    transform: scale(0.85);
  }
}

.switch-cell {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.muted-switch {
  opacity: 0.45;
}

.reset-btn {
  color: var(--senpai-muted);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s;

  &:hover {
    color: var(--senpai-accent-deep);
  }
}

:deep(.el-table__row:hover) .reset-btn {
  opacity: 1;
}
</style>
