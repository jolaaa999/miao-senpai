<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { consoleApi, type CharacterSnapshot } from '@/api/client'

const snap = ref<CharacterSnapshot>({
  profiles: [],
  references: [],
  benchmarks: [],
  traces: [],
  note: '',
  version: 0,
  catalog: '',
})
const loading = ref(false)
const activeTab = ref('profiles')
const expanded = ref<Record<string, boolean>>({})

// 三态核验的展示口径与 data/dl_senpai/characters/README.md 一致：
// 只有人工核验过才能进提示词的硬约束段，模型审阅只能当支撑性提示。
const statusMeta: Record<string, { label: string; type: 'success' | 'warning' | 'info' | 'danger' }> = {
  human_verified: { label: '人工核验', type: 'success' },
  assistant_reviewed: { label: '模型审阅', type: 'warning' },
  unverified: { label: '未核验', type: 'info' },
}

const profileStatusMeta: Record<string, { label: string; type: 'success' | 'warning' | 'info' | 'danger' }> = {
  verified: { label: '全部人工核验', type: 'success' },
  partial_verified: { label: '部分人工核验', type: 'warning' },
  review_only: { label: '仅模型审阅', type: 'warning' },
  draft: { label: '草稿', type: 'info' },
  empty: { label: '无特征', type: 'info' },
}

const totals = computed(() => {
  let human = 0
  let reviewed = 0
  let unverified = 0
  for (const p of snap.value.profiles) {
    human += p.human_verified_count
    reviewed += p.reviewed_count
    unverified += p.unverified_count
  }
  return { human, reviewed, unverified }
})

async function load() {
  loading.value = true
  try {
    const res = await consoleApi.characterIdentity(40)
    snap.value = res.data
    // 默认只展开第一份档案，其余折叠，避免一屏塞满
    const first = res.data.profiles[0]
    if (first) expanded.value[first.key] = true
  } finally {
    loading.value = false
  }
}

function toggle(key: string) {
  expanded.value[key] = !expanded.value[key]
}

function statusTag(status: string) {
  return statusMeta[status] ?? { label: status || '未知', type: 'info' as const }
}

function profileTag(status: string) {
  return profileStatusMeta[status] ?? { label: status || '未知', type: 'info' as const }
}

function seconds(ms: number) {
  if (!ms) return '—'
  return `${(ms / 1000).toFixed(1)}s`
}

onMounted(load)
</script>

<template>
  <div>
    <h1 class="page-title">角色档案</h1>
    <p class="page-subtitle">
      生图时的身份锚点：写错比不写更糟，所以每个视觉特征都带核验状态。
      只有「人工核验」才能进提示词硬约束段，「模型审阅」只能算支撑性提示。
      数据源 <code>src/plugins/dl_senpai/data/characters.json</code>。
    </p>

    <div class="stat-row">
      <div class="stat glass-card">
        <div class="stat-value">{{ snap.profiles.length }}</div>
        <div class="stat-label">收录角色</div>
      </div>
      <div class="stat glass-card">
        <div class="stat-value">{{ totals.human }}</div>
        <div class="stat-label">人工核验字段</div>
      </div>
      <div class="stat glass-card">
        <div class="stat-value">{{ totals.reviewed }}</div>
        <div class="stat-label">仅模型审阅</div>
      </div>
      <div class="stat glass-card">
        <div class="stat-value">{{ totals.unverified }}</div>
        <div class="stat-label">未核验字段</div>
      </div>
      <div class="stat glass-card">
        <div class="stat-value">{{ snap.references.length }}</div>
        <div class="stat-label">参考图条目</div>
      </div>
    </div>

    <div class="toolbar glass-card">
      <el-button @click="load">刷新</el-button>
      <span class="hint">
        复核流程：<code>scripts/character_verify.py</code> 看缺口 → 人工核对原始资料 → 写回 verification_status
      </span>
    </div>

    <el-tabs v-model="activeTab" class="tabs glass-card">
      <el-tab-pane label="角色档案" name="profiles">
        <div v-loading="loading" class="profile-list">
          <div v-for="p in snap.profiles" :key="p.key" class="profile-card glass-card">
            <div class="profile-head" @click="toggle(p.key)">
              <div class="names">
                <span class="localized">{{ p.localized_names.join(' / ') || p.canonical_name }}</span>
                <span class="canonical">{{ p.canonical_name }}</span>
              </div>
              <div class="head-right">
                <el-tag size="small" :type="profileTag(p.profile_status).type">
                  {{ profileTag(p.profile_status).label }}
                </el-tag>
                <el-tag v-if="p.identity_priority" size="small" effect="plain">
                  身份优先级 {{ p.identity_priority }}
                </el-tag>
                <el-tag size="small" effect="plain">可信度 {{ p.confidence }}</el-tag>
                <span class="franchise">{{ p.franchise_localized || p.franchise }}</span>
                <span class="chevron">{{ expanded[p.key] ? '收起' : '展开' }}</span>
              </div>
            </div>

            <div class="counts">
              <span class="dot human">人工 {{ p.human_verified_count }}</span>
              <span class="dot reviewed">审阅 {{ p.reviewed_count }}</span>
              <span class="dot unverified">未核 {{ p.unverified_count }}</span>
              <span v-if="p.aliases.length" class="alias">别名：{{ p.aliases.join('、') }}</span>
            </div>

            <div v-if="expanded[p.key]" class="profile-body">
              <el-table :data="p.fields" size="small">
                <el-table-column prop="label" label="特征" width="110" />
                <el-table-column prop="value" label="内容" />
                <el-table-column label="核验状态" width="110">
                  <template #default="{ row }">
                    <el-tag size="small" :type="statusTag(row.verification_status).type">
                      {{ statusTag(row.verification_status).label }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="备注" width="220">
                  <template #default="{ row }">
                    <span class="note">{{ row.note || row.verification_method || '—' }}</span>
                  </template>
                </el-table-column>
              </el-table>

              <div class="constraints">
                <div class="constraint-block">
                  <span class="label">必须保留</span>
                  <el-tag v-for="m in p.must_preserve" :key="m" size="small" effect="plain">{{ m }}</el-tag>
                </div>
                <div class="constraint-block">
                  <span class="label">可自由改动</span>
                  <el-tag v-for="f in p.creative_freedom" :key="f" size="small" type="info" effect="plain">{{ f }}</el-tag>
                </div>
                <div class="constraint-block">
                  <span class="label">禁止</span>
                  <el-tag v-for="n in p.negative_notes" :key="n" size="small" type="danger" effect="plain">{{ n }}</el-tag>
                </div>
              </div>
            </div>
          </div>
          <div v-if="!snap.profiles.length && !loading" class="empty glass-card">还没有角色档案</div>
        </div>
      </el-tab-pane>

      <el-tab-pane label="A/B 实验" name="bench">
        <p class="tab-desc">
          身份一致性的对照实验：每个文件是同一批请求用一套提示词策略跑出来的结果集，
          用「带人工参考图的条数」判断参考图有没有真正生效（缺失时 bot 会退回纯文本）。
        </p>
        <el-table :data="snap.benchmarks" size="small">
          <el-table-column prop="strategy" label="策略" width="180" />
          <el-table-column prop="item_count" label="样本数" width="90" />
          <el-table-column label="带人工参考图" width="130">
            <template #default="{ row }">
              {{ row.human_reference_count }} / {{ row.item_count }}
            </template>
          </el-table-column>
          <el-table-column prop="avg_prompt_chars" label="平均提示词字数" width="140" />
          <el-table-column label="模型" width="180">
            <template #default="{ row }">{{ row.models.join(', ') || '—' }}</template>
          </el-table-column>
          <el-table-column label="时间范围">
            <template #default="{ row }">{{ row.created_from }} → {{ row.created_to }}</template>
          </el-table-column>
        </el-table>
        <p class="tab-desc">
          结果文件在项目根 <code>bench-*.json</code>，由
          <code>scripts/character_generation_benchmark.py</code> 产出。
        </p>
      </el-tab-pane>

      <el-tab-pane label="生图溯源" name="traces">
        <p class="tab-desc">
          每次生图都会记一条 trace：识别到哪个角色、用了什么参考图模式、耗时与结果。
          写真名角色画歪了，先来这里看当时到底喂了什么。
        </p>
        <el-table :data="snap.traces" size="small">
          <el-table-column prop="ts" label="时间" width="170" />
          <el-table-column label="角色" width="140">
            <template #default="{ row }">{{ row.character || '—' }}</template>
          </el-table-column>
          <el-table-column prop="original_prompt" label="用户原话" />
          <el-table-column label="参考图" width="150">
            <template #default="{ row }">
              {{ row.reference_mode || '—' }}（{{ row.reference_count }}）
            </template>
          </el-table-column>
          <el-table-column prop="generation_mode" label="生成模式" width="120" />
          <el-table-column label="耗时" width="90">
            <template #default="{ row }">{{ seconds(row.elapsed_ms) }}</template>
          </el-table-column>
          <el-table-column label="结果" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.result === 'success' ? 'success' : 'danger'">{{ row.result }}</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <el-tab-pane label="参考图清单" name="refs">
        <p class="tab-desc">
          人工提供的身份参考图（manifest）优先于文字描述。目录里只有 example 示例时这里为空，
          真正启用需要按格式放入 <code>data/dl_senpai/character_references/</code>。
        </p>
        <el-table :data="snap.references" size="small">
          <el-table-column prop="localized_name" label="角色" width="140" />
          <el-table-column prop="file" label="文件" />
          <el-table-column prop="reference_role" label="用途" width="120" />
          <el-table-column prop="priority" label="优先级" width="90" />
          <el-table-column label="核验" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="statusTag(row.verification_status).type">
                {{ statusTag(row.verification_status).label }}
              </el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="notes" label="备注" />
        </el-table>
        <div v-if="!snap.references.length" class="empty glass-card">暂无参考图清单</div>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped lang="scss">
.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 0.75rem;
  margin-bottom: 1rem;
}

.stat {
  padding: 0.85rem 1rem;
  text-align: center;

  .stat-value {
    font-size: 1.4rem;
    font-weight: 700;
    color: var(--senpai-accent-deep);
    font-variant-numeric: tabular-nums;
  }

  .stat-label {
    font-size: 0.74rem;
    color: var(--senpai-muted);
    margin-top: 0.15rem;
  }
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0.85rem 1.1rem;
  margin-bottom: 1rem;
  flex-wrap: wrap;

  .hint {
    font-size: 0.76rem;
    color: var(--senpai-muted);
  }
}

.tabs {
  padding: 0.5rem 1rem 1rem;
}

.tab-desc {
  font-size: 0.78rem;
  color: var(--senpai-muted);
  line-height: 1.6;
  margin: 0.5rem 0 0.75rem;
}

.profile-list {
  display: flex;
  flex-direction: column;
  gap: 0.7rem;
  min-height: 100px;
}

.profile-card {
  padding: 0.8rem 1rem;

  &:hover {
    border-color: rgba(249, 115, 22, 0.28);
  }
}

.profile-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  flex-wrap: wrap;
  cursor: pointer;

  .names {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;

    .localized {
      font-weight: 600;
      color: var(--senpai-text);
      font-size: 0.95rem;
    }

    .canonical {
      font-size: 0.76rem;
      color: var(--senpai-muted);
    }
  }

  .head-right {
    display: flex;
    align-items: center;
    gap: 0.4rem;
    flex-wrap: wrap;

    .franchise {
      font-size: 0.76rem;
      color: var(--senpai-muted);
    }

    .chevron {
      font-size: 0.72rem;
      color: var(--senpai-accent-deep);
    }
  }
}

.counts {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-top: 0.4rem;
  font-size: 0.74rem;
  flex-wrap: wrap;

  .dot {
    padding: 0.05rem 0.4rem;
    border-radius: 5px;

    &.human {
      color: #047857;
      background: rgba(16, 185, 129, 0.14);
    }

    &.reviewed {
      color: #b45309;
      background: rgba(245, 158, 11, 0.16);
    }

    &.unverified {
      color: var(--senpai-muted);
      background: rgba(139, 115, 85, 0.12);
    }
  }

  .alias {
    color: var(--senpai-muted);
  }
}

.profile-body {
  margin-top: 0.75rem;
}

.note {
  font-size: 0.74rem;
  color: var(--senpai-muted);
}

.constraints {
  margin-top: 0.6rem;
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.constraint-block {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.3rem;

  .label {
    font-size: 0.72rem;
    color: var(--senpai-muted);
    min-width: 62px;
  }
}

.empty {
  text-align: center;
  padding: 2rem;
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
