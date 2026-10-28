<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  consoleApi,
  type PersonaCardInfo,
  type PersonaCardsSnapshot,
  type PersonaDraft,
  type SessionSummary,
} from '@/api/client'
import { ElMessage, ElMessageBox } from 'element-plus'

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

// ---------- 人设卡 ----------
// 只做「列 / 绑 / 解绑 / 看正文 / 导出」：编辑卡正文仍直接改 md。
// 体检（AI 味、硬规则位置）在 Python 侧，控制台再写一套规则必然漂移，
// 那种「页面说没问题、线上却开口像客服」的坑不值得挖。
const cards = ref<PersonaCardsSnapshot>({
  dir: '',
  cards: [],
  bindings: {},
  default_id: '',
  note: '',
})
const cardsLoading = ref(false)
const sessions = ref<SessionSummary[]>([])
const bindSession = ref('')
const bodyVisible = ref(false)
const bodyText = ref('')
const bodyTitle = ref('')

const boundSessions = computed(() => {
  const map: Record<string, string[]> = {}
  for (const [session, cardId] of Object.entries(cards.value.bindings || {})) {
    ;(map[cardId] ||= []).push(session)
  }
  return map
})

async function loadCards() {
  cardsLoading.value = true
  try {
    const [cardRes, sessionRes] = await Promise.all([
      consoleApi.personaCards(),
      consoleApi.sessions(),
    ])
    cards.value = cardRes.data
    sessions.value = sessionRes.data.items || []
    if (!bindSession.value && sessions.value.length) {
      bindSession.value = sessions.value[0].session_id
    }
  } catch {
    ElMessage.error('人设卡读取失败')
  } finally {
    cardsLoading.value = false
  }
}

function syncTag(card: PersonaCardInfo) {
  if (card.source_synced === null) return { text: '—', type: 'info' as const }
  return card.source_synced
    ? { text: '= 同步', type: 'success' as const }
    : { text: '≠ 已漂移', type: 'warning' as const }
}

async function bindCard(cardId: string) {
  if (!bindSession.value) {
    ElMessage.warning('先选一个会话（group:群号）')
    return
  }
  try {
    const res = await consoleApi.bindPersonaCard(bindSession.value, cardId)
    cards.value = res.data
    ElMessage.success(`${bindSession.value} 已切到「${cardId}」，下一句就生效`)
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.error || '绑定失败')
  }
}

async function unbindSession(session: string) {
  try {
    const res = await consoleApi.bindPersonaCard(session, '')
    cards.value = res.data
    ElMessage.success(`${session} 已回到 persona.py 那份`)
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.error || '解绑失败')
  }
}

async function showBody(cardId: string) {
  try {
    const res = await consoleApi.personaCardBody(cardId)
    bodyTitle.value = `${res.data.name}（${res.data.file}）`
    bodyText.value = res.data.body
    bodyVisible.value = true
  } catch {
    ElMessage.error('读不到这张卡的正文')
  }
}

async function exportCard() {
  let name = ''
  try {
    const res = await ElMessageBox.prompt(
      '把当前 persona.py 的人设存成一张卡（之后可以在卡上改，不影响其他会话）',
      '导出一张卡',
      { inputPlaceholder: '卡名，例如 学姐2', inputPattern: /^[^\\/:*?"<>|]{1,32}$/, inputErrorMessage: '不能带路径符号' },
    )
    name = res.value
  } catch {
    return
  }
  try {
    const res = await consoleApi.exportPersonaCard(name)
    cards.value = res.data
    ElMessage.success(`已写出 data/dl_senpai/personas/${name}.md`)
  } catch (err: any) {
    ElMessage.error(err?.response?.data?.error || '导出失败')
  }
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

    <section class="glass-card panel full card-panel" v-loading="cardsLoading">
      <h2>人设卡</h2>
      <p class="hint">
        一张卡 = 一份「她是谁」，存在 <code>{{ cards.dir || 'data/dl_senpai/personas' }}</code>。
        绑定某个会话后该会话就用卡；没绑定的继续用 <code>persona.py</code> 那份。
        这里的绑定即时生效，不用重启（改 .env 默认卡才要重启）。
      </p>
      <div class="card-toolbar">
        <el-select
          v-model="bindSession"
          filterable
          allow-create
          default-first-option
          placeholder="选择会话（group:群号 / private:QQ）"
          class="session-pick"
        >
          <el-option
            v-for="s in sessions"
            :key="s.session_id"
            :label="`${s.session_id}（${s.turn_count} 轮）`"
            :value="s.session_id"
          />
        </el-select>
        <el-button @click="loadCards">刷新</el-button>
        <el-button type="primary" plain @click="exportCard">把当前人设导出成卡</el-button>
      </div>

      <el-empty v-if="!cards.cards.length" description="卡库是空的：先放个 md 进去，或用上面的导出按钮" />

      <el-table v-else :data="cards.cards" size="small" class="card-table">
        <el-table-column prop="id" label="卡名" width="150" />
        <el-table-column prop="name" label="展示名" width="130" />
        <el-table-column label="来源" width="120">
          <template #default="{ row }">
            <el-tag :type="syncTag(row).type" size="small">{{ syncTag(row).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="chars" label="字数" width="80" />
        <el-table-column label="绑定的会话" min-width="220">
          <template #default="{ row }">
            <template v-if="boundSessions[row.id]">
              <el-tag
                v-for="session in boundSessions[row.id]"
                :key="session"
                size="small"
                closable
                class="session-tag"
                @close="unbindSession(session)"
              >
                {{ session }}
              </el-tag>
            </template>
            <span v-else class="muted">未绑定</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <el-button link type="primary" @click="showBody(row.id)">看正文</el-button>
            <el-button link type="primary" @click="bindCard(row.id)">绑到选中会话</el-button>
          </template>
        </el-table-column>
      </el-table>

      <p class="hint card-foot">
        默认卡：<code>{{ cards.default_id || '（无，未绑定的会话用 persona.py）' }}</code>
        <br />
        群里也能用 <code>/人设 &lt;卡名&gt;</code> 切换（需管理员）；「≠ 已漂移」表示这张卡是从
        persona.py 导出后又改过（或源头后来改过），改哪份算数得先定。
      </p>

      <el-dialog v-model="bodyVisible" :title="bodyTitle" width="72%">
        <pre class="card-body">{{ bodyText }}</pre>
      </el-dialog>
    </section>

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

.card-panel {
  margin-bottom: 1rem;
}

.card-toolbar {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  margin-bottom: 0.75rem;
  flex-wrap: wrap;
}

.session-pick {
  min-width: 260px;
}

.card-table {
  margin-bottom: 0.5rem;
}

.session-tag {
  margin-right: 0.35rem;
}

.muted {
  color: var(--senpai-muted);
  font-size: 0.85rem;
}

.card-foot {
  margin-top: 0.5rem;
}

.card-body {
  white-space: pre-wrap;
  word-break: break-word;
  font-family: 'Cascadia Code', 'Consolas', monospace;
  font-size: 0.82rem;
  line-height: 1.6;
  max-height: 60vh;
  overflow: auto;
  margin: 0;
}
</style>
