<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { consoleApi, type SpeakStyle, type SpeakStylesSnapshot } from '@/api/client'

const loading = ref(true)
const saving = ref(false)
const snap = ref<SpeakStylesSnapshot | null>(null)

const draft = reactive<SpeakStyle>({
  id: '',
  name: '',
  kind: 'finetune',
  enabled: true,
  model: '',
  base_url: '',
  api_key: '',
  system_overlay: '',
  source_person: '',
  qq_data_note: '',
  trained_model_ref: '',
  notes: '',
  updated_at: 0,
})

const activeName = computed(() => snap.value?.active?.name || '—')
const activeId = computed(() => snap.value?.active_id || '')

async function load() {
  loading.value = true
  try {
    const res = await consoleApi.speakStyles()
    snap.value = res.data
  } catch {
    ElMessage.error('加载语言风格失败')
  } finally {
    loading.value = false
  }
}

function edit(row: SpeakStyle) {
  Object.assign(draft, { ...row })
}

function resetDraft() {
  Object.assign(draft, {
    id: '',
    name: '',
    kind: 'finetune',
    enabled: true,
    model: '',
    base_url: '',
    api_key: '',
    system_overlay: '',
    source_person: '',
    qq_data_note: '',
    trained_model_ref: '',
    notes: '',
    updated_at: 0,
  })
}

async function save() {
  if (!draft.id.trim() || !draft.name.trim()) {
    ElMessage.warning('请填写 id 与名称')
    return
  }
  saving.value = true
  try {
    const res = await consoleApi.upsertSpeakStyle({ ...draft })
    snap.value = res.data
    ElMessage.success('已保存风格（bot 下次回复即按当前激活风格走模型）')
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.error || '保存失败')
  } finally {
    saving.value = false
  }
}

async function activate(id: string) {
  try {
    const res = await consoleApi.setSpeakStyleActive(id)
    snap.value = res.data
    ElMessage.success(`已切换为：${res.data.active.name}`)
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.error || '切换失败')
  }
}

async function remove(id: string) {
  try {
    await ElMessageBox.confirm(`确定删除风格「${id}」？`, '删除确认', { type: 'warning' })
    const res = await consoleApi.deleteSpeakStyle(id)
    snap.value = res.data
    if (draft.id === id) resetDraft()
    ElMessage.success('已删除')
  } catch (e: any) {
    if (e === 'cancel' || e === 'close') return
    ElMessage.error(e?.response?.data?.error || '删除失败')
  }
}

function kindLabel(k: string) {
  if (k === 'builtin') return '内置'
  if (k === 'finetune') return '微调模型'
  if (k === 'prompt') return '仅提示词'
  return k || '—'
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <div class="page-head">
      <div>
        <h1 class="page-title">语言风格</h1>
        <p class="page-subtitle">
          登记 QQ 聊天记录微调后的模型，在此切换。当前生效：
          <strong class="active-pill">{{ activeName }}</strong>
          <code v-if="activeId">({{ activeId }})</code>
        </p>
      </div>
      <div class="head-actions">
        <el-button @click="load">刷新</el-button>
        <el-button @click="resetDraft">新建空白</el-button>
      </div>
    </div>

    <el-alert
      type="info"
      :closable="false"
      show-icon
      class="hint-alert"
      title="真微调在外部完成：QQ 导出 → MirrorFlow/WeClone 清洗训练 → 部署到 OpenAI 兼容端点 → 下方填写 model/base_url 并激活。"
    />

    <div class="grid">
      <section class="glass-card panel">
        <h2>风格列表</h2>
        <el-table :data="snap?.styles || []" size="small" style="width: 100%">
          <el-table-column label="状态" width="88">
            <template #default="{ row }">
              <el-tag v-if="row.id === activeId" type="success" size="small">使用中</el-tag>
              <el-tag v-else-if="!row.enabled" type="info" size="small">禁用</el-tag>
              <span v-else class="muted">—</span>
            </template>
          </el-table-column>
          <el-table-column prop="name" label="名称" min-width="120" />
          <el-table-column prop="id" label="ID" min-width="110" />
          <el-table-column label="类型" width="100">
            <template #default="{ row }">{{ kindLabel(row.kind) }}</template>
          </el-table-column>
          <el-table-column prop="model" label="模型" min-width="140" show-overflow-tooltip />
          <el-table-column prop="source_person" label="模仿对象" min-width="100" show-overflow-tooltip />
          <el-table-column label="操作" width="220" fixed="right">
            <template #default="{ row }">
              <el-button
                link
                type="primary"
                :disabled="row.id === activeId || !row.enabled"
                @click="activate(row.id)"
              >
                启用
              </el-button>
              <el-button link @click="edit(row)">编辑</el-button>
              <el-button
                link
                type="danger"
                :disabled="row.id === 'senpai_default'"
                @click="remove(row.id)"
              >
                删除
              </el-button>
            </template>
          </el-table-column>
        </el-table>
        <p class="meta">配置文件：{{ snap?.path }}</p>
      </section>

      <section class="glass-card panel">
        <h2>{{ draft.id ? `编辑：${draft.name || draft.id}` : '新建 / 编辑风格' }}</h2>
        <el-form label-position="top">
          <div class="two-col">
            <el-form-item label="ID（英文/数字/下划线）">
              <el-input v-model="draft.id" :disabled="draft.id === 'senpai_default'" placeholder="e.g. alice_ft" />
            </el-form-item>
            <el-form-item label="显示名称">
              <el-input v-model="draft.name" placeholder="Alice 风格" />
            </el-form-item>
          </div>
          <div class="two-col">
            <el-form-item label="类型">
              <el-select v-model="draft.kind" style="width: 100%">
                <el-option label="微调模型 finetune" value="finetune" />
                <el-option label="仅提示词 prompt" value="prompt" />
                <el-option label="内置 builtin" value="builtin" :disabled="draft.id !== 'senpai_default'" />
              </el-select>
            </el-form-item>
            <el-form-item label="启用">
              <el-switch v-model="draft.enabled" />
            </el-form-item>
          </div>
          <el-form-item label="模型名 model（微调部署后的 id；空=跟全局 .env）">
            <el-input v-model="draft.model" placeholder="gpt-6-astra / cm-xxx / 本地 LoRA 服务名" />
          </el-form-item>
          <el-form-item label="Base URL（空=跟全局中转站）">
            <el-input v-model="draft.base_url" placeholder="https://airelay.buzz/v1" />
          </el-form-item>
          <el-form-item label="API Key（空=跟全局；仅保存在本地 styles.json）">
            <el-input v-model="draft.api_key" type="password" show-password placeholder="可选" />
          </el-form-item>
          <div class="two-col">
            <el-form-item label="模仿对象备注">
              <el-input v-model="draft.source_person" placeholder="谁的说话风格" />
            </el-form-item>
            <el-form-item label="训练产物引用">
              <el-input v-model="draft.trained_model_ref" placeholder="LoRA 名 / 方舟 cm-xxx" />
            </el-form-item>
          </div>
          <el-form-item label="QQ 数据 / 训练备注">
            <el-input v-model="draft.qq_data_note" type="textarea" :rows="2" placeholder="导出源、条数、清洗说明…" />
          </el-form-item>
          <el-form-item label="风格附加提示（会拼进 system；微调模型也可写轻提示）">
            <el-input
              v-model="draft.system_overlay"
              type="textarea"
              :rows="5"
              class="mono"
              placeholder="用词更短、爱用……、不要用学姐那套傲娇口癖……"
            />
          </el-form-item>
          <el-form-item label="备注">
            <el-input v-model="draft.notes" />
          </el-form-item>
          <el-button type="primary" :loading="saving" @click="save">保存风格</el-button>
        </el-form>
      </section>
    </div>
  </div>
</template>

<style scoped lang="scss">
.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
  margin-bottom: 16px;
}
.head-actions {
  display: flex;
  gap: 8px;
}
.active-pill {
  color: var(--el-color-success);
}
.hint-alert {
  margin-bottom: 16px;
}
.grid {
  display: grid;
  gap: 16px;
}
.panel {
  padding: 18px 20px;
  h2 {
    margin: 0 0 12px;
    font-size: 16px;
  }
}
.two-col {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}
.meta,
.muted {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin-top: 10px;
}
.mono :deep(textarea) {
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 13px;
}
@media (max-width: 900px) {
  .two-col {
    grid-template-columns: 1fr;
  }
  .page-head {
    flex-direction: column;
  }
}
</style>
