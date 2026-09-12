<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { consoleApi, type AffectionGroup, type AffectionUser } from '@/api/client'
import { ElMessage } from 'element-plus'

const TIER_LADDER = [
  { name: '路人', threshold: 0 },
  { name: '面熟', threshold: 15 },
  { name: '常来', threshold: 40 },
  { name: '亲近', threshold: 80 },
  { name: '信赖', threshold: 150 },
  { name: '心头好', threshold: 300 },
  { name: '热恋', threshold: 450 },
  { name: '眷侣', threshold: 700 },
] as const

const groups = ref<AffectionGroup[]>([])
const selectedGroup = ref('')
const scopeFilter = ref<'all' | 'group' | 'private'>('all')
const keyword = ref('')
const users = ref<AffectionUser[]>([])
const loading = ref(false)
const editVisible = ref(false)
const editing = ref<AffectionUser | null>(null)
const editForm = ref({ value: 0, display_name: '', tier_title: '' })

const tierTagType = (tier: string) => {
  const map: Record<string, string> = {
    路人: 'info',
    面熟: '',
    常来: 'success',
    亲近: 'warning',
    信赖: 'danger',
    心头好: 'danger',
    热恋: 'danger',
    眷侣: 'danger',
  }
  return map[tier] || ''
}

function tierThreshold(name: string) {
  return TIER_LADDER.find((t) => t.name === name)?.threshold ?? 0
}

function progressPercent(row: AffectionUser) {
  if (!row.next_tier_name) return 100
  const prev = tierThreshold(row.tier_name)
  const span = (row.next_tier_threshold ?? 0) - prev
  if (span <= 0) return 100
  return Math.min(100, Math.round(((row.value - prev) / span) * 100))
}

function scopeLabel(g: AffectionGroup) {
  if (g.scope_type === 'private') {
    return `私聊 ${g.group_id.replace(/^private_/, '')}`
  }
  return `群 ${g.group_id}（${g.user_count} 人）`
}

const isPrivateScope = computed(
  () => groups.value.find((g) => g.group_id === selectedGroup.value)?.scope_type === 'private',
)

const filteredGroups = computed(() => {
  if (scopeFilter.value === 'all') return groups.value
  return groups.value.filter((g) => g.scope_type === scopeFilter.value)
})

const groupScopeCount = computed(() => groups.value.filter((g) => g.scope_type === 'group').length)
const privateScopeCount = computed(() => groups.value.filter((g) => g.scope_type === 'private').length)
const totalUsersInScopes = computed(() => groups.value.reduce((s, g) => s + g.user_count, 0))

const currentScopeMeta = computed(() => groups.value.find((g) => g.group_id === selectedGroup.value))

const filteredUsers = computed(() => {
  const q = keyword.value.trim().toLowerCase()
  if (!q) return users.value
  return users.value.filter(
    (u) =>
      u.user_id.includes(q) ||
      (u.display_name || '').toLowerCase().includes(q) ||
      (u.tier_title || '').includes(q) ||
      u.tier_name.includes(q),
  )
})

const avgValue = computed(() => {
  if (!users.value.length) return 0
  return Math.round(users.value.reduce((s, u) => s + u.value, 0) / users.value.length)
})

const maxValue = computed(() => {
  if (!users.value.length) return 0
  return Math.max(...users.value.map((u) => u.value))
})

async function loadGroups() {
  const res = await consoleApi.affectionGroups()
  groups.value = res.data.items
  const pool = filteredGroups.value
  if (!pool.length) {
    selectedGroup.value = ''
    users.value = []
    return
  }
  if (!pool.some((g) => g.group_id === selectedGroup.value)) {
    selectedGroup.value = pool[0].group_id
  }
}

async function loadUsers() {
  if (!selectedGroup.value) {
    users.value = []
    return
  }
  loading.value = true
  try {
    const res = await consoleApi.affectionUsers(selectedGroup.value)
    users.value = res.data.items.sort((a, b) => b.value - a.value)
  } finally {
    loading.value = false
  }
}

function openEdit(row: AffectionUser) {
  editing.value = row
  editForm.value = {
    value: row.value,
    display_name: row.display_name,
    tier_title: row.tier_title || '',
  }
  editVisible.value = true
}

async function saveEdit() {
  if (!editing.value || !selectedGroup.value) return
  await consoleApi.patchAffectionUser(selectedGroup.value, editing.value.user_id, {
    value: editForm.value.value,
    display_name: editForm.value.display_name,
    tier_title: editForm.value.tier_title,
  })
  ElMessage.success('已保存')
  editVisible.value = false
  await loadUsers()
  await loadGroups()
}

watch(scopeFilter, async () => {
  await loadGroups()
  await loadUsers()
})

onMounted(async () => {
  await loadGroups()
  await loadUsers()
})

watch(selectedGroup, loadUsers)
</script>

<template>
  <div>
    <h1 class="page-title">好感管理</h1>
    <p class="page-subtitle">
      群内好感与私聊好感<strong>分开存储</strong>；数据目录
      <code>data/dl_senpai/affection/group_*.json</code>
    </p>

    <div class="stat-grid">
      <div class="stat-card glass-card">
        <span class="stat-icon">👥</span>
        <div>
          <div class="stat-value">{{ groupScopeCount }}</div>
          <div class="stat-label">群好感档案</div>
        </div>
      </div>
      <div class="stat-card glass-card">
        <span class="stat-icon">💬</span>
        <div>
          <div class="stat-value">{{ privateScopeCount }}</div>
          <div class="stat-label">私聊好感档案</div>
        </div>
      </div>
      <div class="stat-card glass-card">
        <span class="stat-icon">📋</span>
        <div>
          <div class="stat-value">{{ totalUsersInScopes }}</div>
          <div class="stat-label">总记录条数</div>
        </div>
      </div>
      <div class="stat-card glass-card">
        <span class="stat-icon">💕</span>
        <div>
          <div class="stat-value">{{ selectedGroup ? avgValue : '—' }}</div>
          <div class="stat-label">当前范围均好感</div>
        </div>
      </div>
    </div>

    <div class="toolbar glass-card">
      <div class="toolbar-row">
        <span class="label">范围类型</span>
        <el-radio-group v-model="scopeFilter" size="small">
          <el-radio-button value="all">全部</el-radio-button>
          <el-radio-button value="group">仅群</el-radio-button>
          <el-radio-button value="private">仅私聊</el-radio-button>
        </el-radio-group>
      </div>
      <div class="toolbar-row">
        <span class="label">选择范围</span>
        <el-select
          v-model="selectedGroup"
          placeholder="选择群或私聊档案"
          style="width: 280px"
          filterable
        >
          <el-option
            v-for="g in filteredGroups"
            :key="g.group_id"
            :label="scopeLabel(g)"
            :value="g.group_id"
          >
            <div class="option-line">
              <el-tag size="small" :type="g.scope_type === 'private' ? 'success' : 'warning'">
                {{ g.scope_type === 'private' ? '私聊' : '群' }}
              </el-tag>
              <span>{{ scopeLabel(g) }}</span>
            </div>
          </el-option>
        </el-select>
        <el-input
          v-model="keyword"
          placeholder="搜索昵称 / QQ / 档位 / 头衔"
          clearable
          style="width: 240px"
        />
        <el-button @click="loadGroups(); loadUsers()">刷新</el-button>
      </div>
      <div v-if="currentScopeMeta" class="scope-hint">
        <el-tag size="small" :type="isPrivateScope ? 'success' : 'warning'">
          {{ isPrivateScope ? '私聊好感' : '群好感' }}
        </el-tag>
        <span v-if="isPrivateScope">
          私聊仅通过和学姐聊天涨好感；无群排行、不同步群专属头衔。
        </span>
        <span v-else>
          签到 / 任务 / 商店 / @聊天 均可涨好感；升档可同步群专属头衔。
        </span>
        <span v-if="users.length" class="muted-inline">
          · 当前 {{ users.length }} 人 · 最高 {{ maxValue }} 点
        </span>
      </div>
    </div>

    <div class="content-grid">
      <div class="table-wrap glass-card">
        <el-table v-loading="loading" :data="filteredUsers" stripe style="width: 100%">
          <el-table-column prop="display_name" label="昵称" min-width="110" />
          <el-table-column prop="user_id" label="QQ" width="130" />
          <el-table-column prop="value" label="好感" width="80" sortable />
          <el-table-column :label="isPrivateScope ? '专属称谓' : '群头衔'" width="120">
            <template #default="{ row }">
              <span v-if="row.tier_title">{{ row.tier_title }}</span>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column label="档位" width="100">
            <template #default="{ row }">
              <el-tag size="small" :type="tierTagType(row.tier_name)">{{ row.tier_name }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="升档进度" min-width="190">
            <template #default="{ row }">
              <template v-if="row.next_tier_name">
                <el-progress
                  :percentage="progressPercent(row)"
                  :stroke-width="10"
                  :format="() => `距「${row.next_tier_name}」${row.points_to_next}`"
                />
              </template>
              <span v-else class="muted">已满级「眷侣」</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="80" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="!filteredGroups.length && !loading" class="empty">暂无好感数据文件</div>
        <div v-else-if="!selectedGroup" class="empty">请选择群或私聊档案</div>
        <div v-else-if="!filteredUsers.length && !loading" class="empty">
          {{ keyword ? '没有匹配的记录' : '该范围暂无好感记录' }}
        </div>
      </div>

      <aside class="tier-panel glass-card">
        <h2>档位阶梯</h2>
        <p class="tier-note">群内与私聊共用同一套档位与语气规则。</p>
        <ul class="tier-list">
          <li v-for="t in TIER_LADDER" :key="t.name">
            <span class="tier-name">{{ t.name }}</span>
            <span class="tier-th">{{ t.threshold }}+</span>
          </li>
        </ul>
        <h3>群内专属</h3>
        <ul class="feature-list">
          <li>签到 / 任务加分</li>
          <li>商店购买 / 换装加分</li>
          <li>商店好感特惠折扣</li>
          <li>升档同步群专属头衔</li>
        </ul>
        <h3>私聊专属</h3>
        <ul class="feature-list">
          <li>每次私聊聊天加分</li>
          <li>独立档案，不与群混算</li>
        </ul>
      </aside>
    </div>

    <el-dialog v-model="editVisible" title="编辑好感" width="440px">
      <el-form label-width="88px">
        <el-form-item label="QQ">
          <el-input :model-value="editing?.user_id" disabled />
        </el-form-item>
        <el-form-item label="昵称">
          <el-input v-model="editForm.display_name" placeholder="展示用昵称" />
        </el-form-item>
        <el-form-item label="好感">
          <el-input-number v-model="editForm.value" :min="0" :step="5" />
        </el-form-item>
        <el-form-item :label="isPrivateScope ? '专属称谓' : '群头衔'">
          <el-input
            v-model="editForm.tier_title"
            :placeholder="isPrivateScope ? '私聊档位称谓（可选）' : '群专属头衔（需机器人群管同步）'"
            maxlength="12"
            show-word-limit
          />
        </el-form-item>
        <p v-if="editing" class="hint">
          保存后档位「{{ editing.tier_name }}」会按好感值自动重算；改头衔不会自动同步到 QQ，需在群内再次触发升档或手动设置。
        </p>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped lang="scss">
.stat-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 1rem;
  margin-bottom: 1rem;

  @media (max-width: 960px) {
    grid-template-columns: repeat(2, 1fr);
  }
}

.stat-card {
  display: flex;
  align-items: center;
  gap: 0.85rem;
  padding: 1rem 1.15rem;
}

.stat-icon {
  font-size: 1.6rem;
}

.stat-value {
  font-size: 1.35rem;
  font-weight: 700;
  color: #fb7185;
}

.stat-label {
  font-size: 0.82rem;
  color: var(--senpai-muted);
}

.toolbar {
  padding: 1rem 1.25rem;
  margin-bottom: 1rem;
  display: flex;
  flex-direction: column;
  gap: 0.85rem;
}

.toolbar-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.75rem;
}

.label {
  color: var(--senpai-muted);
  font-size: 0.9rem;
  min-width: 4.5rem;
}

.scope-hint {
  font-size: 0.85rem;
  color: var(--senpai-muted);
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
  padding-top: 0.25rem;
  border-top: 1px solid var(--senpai-border, rgba(0, 0, 0, 0.06));
}

.muted-inline {
  opacity: 0.85;
}

.option-line {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.content-grid {
  display: grid;
  grid-template-columns: 1fr 260px;
  gap: 1rem;
  align-items: start;

  @media (max-width: 1100px) {
    grid-template-columns: 1fr;
  }
}

.table-wrap {
  padding: 0.5rem;
  overflow: hidden;
  min-width: 0;
}

.tier-panel {
  padding: 1rem 1.1rem;

  h2 {
    margin: 0 0 0.35rem;
    font-size: 1rem;
  }

  h3 {
    margin: 1rem 0 0.4rem;
    font-size: 0.9rem;
    color: var(--senpai-muted);
  }
}

.tier-note {
  margin: 0 0 0.75rem;
  font-size: 0.8rem;
  color: var(--senpai-muted);
}

.tier-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.tier-list li {
  display: flex;
  justify-content: space-between;
  padding: 0.35rem 0;
  font-size: 0.88rem;
  border-bottom: 1px dashed var(--senpai-border, rgba(0, 0, 0, 0.06));
}

.tier-th {
  color: var(--senpai-muted);
  font-variant-numeric: tabular-nums;
}

.feature-list {
  margin: 0;
  padding-left: 1.1rem;
  font-size: 0.82rem;
  color: var(--senpai-muted);
  line-height: 1.65;
}

.muted {
  color: var(--senpai-muted);
  font-size: 0.85rem;
}

.empty {
  text-align: center;
  padding: 2.5rem;
  color: var(--senpai-muted);
}

.hint {
  margin: 0 0 0 88px;
  font-size: 0.8rem;
  color: var(--senpai-muted);
  line-height: 1.5;
}

code {
  font-size: 0.85em;
  color: var(--senpai-accent-deep);
  background: var(--senpai-accent-soft);
  padding: 0.1rem 0.35rem;
  border-radius: 4px;
}
</style>
