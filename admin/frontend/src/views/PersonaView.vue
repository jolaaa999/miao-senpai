<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { consoleApi, type PersonaDraft } from '@/api/client'
import { ElMessage } from 'element-plus'

const loading = ref(true)
const saving = ref(false)
const form = ref<PersonaDraft>({
  senpai_name: '',
  system_prompt: '',
  appearance: '',
  monster_hunter_extra: '',
  file_path: '',
  updated_at: '',
})

async function load() {
  loading.value = true
  try {
    const res = await consoleApi.personaDraft()
    form.value = res.data
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  try {
    const res = await consoleApi.savePersonaDraft({
      senpai_name: form.value.senpai_name,
      system_prompt: form.value.system_prompt,
      appearance: form.value.appearance,
      monster_hunter_extra: form.value.monster_hunter_extra,
    })
    form.value = res.data
    ElMessage.success('已保存到 persona.py，下一轮对话自动生效')
  } catch {
    ElMessage.error('保存失败')
  } finally {
    saving.value = false
  }
}

function formatTime(iso: string) {
  if (!iso) return '—'
  return new Date(iso).toLocaleString('zh-CN')
}

onMounted(load)
</script>

<template>
  <div v-loading="loading">
    <div class="page-head">
      <div>
        <h1 class="page-title">学姐自设</h1>
        <p class="page-subtitle">
          编辑人设提示词，保存后直接写入
          <code>{{ form.file_path || 'src/plugins/dl_senpai/persona.py' }}</code>
          ，bot 会在下次回复时自动热加载。
        </p>
      </div>
      <div class="head-actions">
        <el-button @click="load">重新加载</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存到文件</el-button>
      </div>
    </div>

    <div class="persona-grid">
      <section class="glass-card panel">
        <h2>基础信息</h2>
        <el-form label-position="top">
          <el-form-item label="自称 / 昵称（SENPAI_NAME）">
            <el-input v-model="form.senpai_name" placeholder="学姐" maxlength="32" show-word-limit />
          </el-form-item>
          <el-form-item label="文件更新时间">
            <span class="meta">{{ formatTime(form.updated_at) }}</span>
          </el-form-item>
        </el-form>
      </section>

      <section class="glass-card panel appearance-panel">
        <h2>外貌自设</h2>
        <p class="hint">对应人设里的【外貌自设】段落，生图/自拍时会参考这里。</p>
        <el-input
          v-model="form.appearance"
          type="textarea"
          :rows="8"
          placeholder="- 粉长发、狐耳狐尾…"
        />
      </section>

      <section class="glass-card panel full">
        <h2>核心人设（SYSTEM_PROMPT）</h2>
        <p class="hint">
          学姐的基础性格、说话方式、能力边界、表情包/语音/身份保密等。保存时会自动把上方「外貌自设」合并进此段。
        </p>
        <el-input v-model="form.system_prompt" type="textarea" :rows="22" class="mono" />
      </section>

      <section class="glass-card panel full">
        <h2>怪物猎人专长（MONSTER_HUNTER_SYSTEM_EXTRA）</h2>
        <p class="hint">怪猎相关对话时追加的专长说明，可与核心人设分开调整。</p>
        <el-input v-model="form.monster_hunter_extra" type="textarea" :rows="10" class="mono" />
      </section>

      <section class="glass-card panel full readonly-note">
        <h2>系统固定模块（只读）</h2>
        <p class="hint">
          插嘴模式、被 @ 模式、识图、群名片、禁言、联网检索、生图等模块仍在
          <code>persona.py</code> 中由功能开关动态拼接，此处不提供编辑。若需修改请直接改源码。
        </p>
      </section>
    </div>
  </div>
</template>

<style scoped lang="scss">
.page-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
  margin-bottom: 1rem;
  flex-wrap: wrap;
}

.head-actions {
  display: flex;
  gap: 0.5rem;
}

.persona-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;

  @media (max-width: 900px) {
    grid-template-columns: 1fr;
  }
}

.panel {
  padding: 1.25rem;

  h2 {
    margin: 0 0 0.75rem;
    font-size: 1rem;
    color: var(--senpai-accent-deep);
    font-weight: 600;
  }

  &.full {
    grid-column: 1 / -1;
  }
}

.hint {
  margin: 0 0 0.75rem;
  font-size: 0.82rem;
  color: var(--senpai-muted);
  line-height: 1.5;

  code {
    color: var(--senpai-accent-deep);
    background: var(--senpai-accent-soft);
    padding: 0.1rem 0.35rem;
    border-radius: 4px;
  }
}

.meta {
  color: var(--senpai-muted);
  font-size: 0.9rem;
}

.mono :deep(textarea) {
  font-family: 'Cascadia Code', 'Consolas', 'DM Sans', monospace;
  font-size: 0.82rem;
  line-height: 1.55;
}

.readonly-note {
  background: rgba(255, 255, 255, 0.55);
}
</style>
