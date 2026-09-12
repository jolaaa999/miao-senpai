<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { consoleApi, type CheckinUser, type AffectionUser } from '@/api/client'
import { ElMessage } from 'element-plus'

const groups = ref<{ group_id: string; user_count: number }[]>([])
const selectedGroup = ref('')
const users = ref<CheckinUser[]>([])
const affectionMap = ref<Record<string, AffectionUser>>({})
const loading = ref(false)
const editVisible = ref(false)
const editing = ref<CheckinUser | null>(null)
const editForm = ref({ points: 0, streak: 0, title: '', display_name: '' })

async function loadGroups() {
  const res = await consoleApi.checkinGroups()
  groups.value = res.data.items
  if (!selectedGroup.value && groups.value.length) {
    selectedGroup.value = groups.value[0].group_id
  }
}

async function loadUsers() {
  if (!selectedGroup.value) return
  loading.value = true
  try {
    const [checkinRes, affectionRes] = await Promise.all([
      consoleApi.checkinUsers(selectedGroup.value),
      consoleApi.affectionUsers(selectedGroup.value).catch(() => ({ data: { items: [] as AffectionUser[] } })),
    ])
    users.value = checkinRes.data.items.sort((a, b) => b.points - a.points)
    const map: Record<string, AffectionUser> = {}
    for (const u of affectionRes.data.items) {
      map[u.user_id] = u
    }
    affectionMap.value = map
  } finally {
    loading.value = false
  }
}

function openEdit(row: CheckinUser) {
  editing.value = row
  editForm.value = {
    points: row.points,
    streak: row.streak,
    title: row.title,
    display_name: row.display_name,
  }
  editVisible.value = true
}

async function saveEdit() {
  if (!editing.value || !selectedGroup.value) return
  await consoleApi.patchCheckinUser(selectedGroup.value, editing.value.user_id, editForm.value)
  ElMessage.success('已保存')
  editVisible.value = false
  await loadUsers()
}

onMounted(async () => {
  await loadGroups()
  await loadUsers()
})

watch(selectedGroup, loadUsers)
</script>

<template>
  <div>
    <h1 class="page-title">签到管理</h1>
    <p class="page-subtitle">查看与调整群签到积分、连签、称号；好感列可跳转「好感管理」细查</p>

    <div class="toolbar glass-card">
      <span>选择群：</span>
      <el-select v-model="selectedGroup" placeholder="选择签到群" style="width: 220px">
        <el-option
          v-for="g in groups"
          :key="g.group_id"
          :label="`群 ${g.group_id}（${g.user_count} 人）`"
          :value="g.group_id"
        />
      </el-select>
      <el-button @click="loadUsers">刷新</el-button>
    </div>

    <div class="table-wrap glass-card">
      <el-table v-loading="loading" :data="users" stripe style="width: 100%">
        <el-table-column prop="display_name" label="昵称" min-width="120" />
        <el-table-column prop="user_id" label="QQ" width="140" />
        <el-table-column prop="points" label="积分" width="90" sortable />
        <el-table-column label="好感" width="120">
          <template #default="{ row }">
            <template v-if="affectionMap[row.user_id]">
              <span>{{ affectionMap[row.user_id].value }}</span>
              <el-tag size="small" class="aff-tag">{{ affectionMap[row.user_id].tier_name }}</el-tag>
            </template>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column prop="streak" label="连签" width="80" />
        <el-table-column prop="max_streak" label="最高连签" width="100" />
        <el-table-column prop="title" label="称号" min-width="140" />
        <el-table-column prop="last_checkin" label="上次签到" width="120" />
        <el-table-column label="今日任务" min-width="200">
          <template #default="{ row }">
            <span v-if="row.pending_task" class="task-text">{{ row.pending_task.text }}</span>
            <span v-else class="muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" @click="openEdit(row)">编辑</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <el-dialog v-model="editVisible" title="编辑用户" width="420px">
      <el-form label-width="80px">
        <el-form-item label="昵称">
          <el-input v-model="editForm.display_name" />
        </el-form-item>
        <el-form-item label="积分">
          <el-input-number v-model="editForm.points" :min="0" />
        </el-form-item>
        <el-form-item label="连签">
          <el-input-number v-model="editForm.streak" :min="0" />
        </el-form-item>
        <el-form-item label="称号">
          <el-input v-model="editForm.title" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editVisible = false">取消</el-button>
        <el-button type="primary" @click="saveEdit">保存</el-button>
      </template>
    </el-dialog>
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
    font-size: 0.9rem;
  }
}

.table-wrap {
  padding: 0.5rem;
  overflow: hidden;
}

.task-text {
  font-size: 0.8rem;
  color: var(--senpai-muted);
}

.muted {
  color: var(--senpai-muted);
}

.aff-tag {
  margin-left: 0.35rem;
  vertical-align: middle;
}
</style>
