<script setup lang="ts">
// 更新记录页：内容全部来自 config/changelog.ts，改版本只改那一处数据文件。
import { CHANGELOG, CONSOLE_VERSION } from '@/config/changelog'

// 最新一版高亮，方便一眼看到「这次动了什么」
const latestVersion = CONSOLE_VERSION

function dayLabel(date: string) {
  return date || '—'
}
</script>

<template>
  <div>
    <h1 class="page-title">更新记录</h1>
    <p class="page-subtitle">
      当前控制台版本 <strong>v{{ CONSOLE_VERSION }}</strong>。每次改动都按语义化版本记一条：
      major=整体重做，minor=新增页面或可写能力，patch=修 bug / 改文案。
      记录维护在 <code>src/config/changelog.ts</code>，新增页面时同一提交里补一条。
    </p>

    <div class="timeline">
      <div
        v-for="entry in CHANGELOG"
        :key="entry.version"
        class="entry glass-card"
        :class="{ latest: entry.version === latestVersion }"
      >
        <div class="entry-head">
          <div class="version-badge">v{{ entry.version }}</div>
          <div class="entry-title">
            <span class="title-text">{{ entry.title }}</span>
            <el-tag v-if="entry.version === latestVersion" size="small" type="warning">当前版本</el-tag>
          </div>
          <div class="entry-date">{{ dayLabel(entry.date) }}</div>
        </div>

        <ul class="highlights">
          <li v-for="(h, i) in entry.highlights" :key="i">{{ h }}</li>
        </ul>

        <el-table :data="entry.pages" size="small" class="pages-table">
          <el-table-column prop="name" label="页面" width="140" />
          <el-table-column label="变更" width="90">
            <template #default="{ row }">
              <el-tag size="small" :type="row.kind === '新增' ? 'success' : 'info'">{{ row.kind }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column prop="desc" label="说明" />
        </el-table>

        <div v-if="entry.api?.length" class="foot-row">
          <span class="foot-label">接口</span>
          <code v-for="a in entry.api" :key="a" class="api-chip">{{ a }}</code>
        </div>
        <div v-if="entry.commits?.length" class="foot-row">
          <span class="foot-label">commit</span>
          <code v-for="c in entry.commits" :key="c" class="api-chip">{{ c }}</code>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.timeline {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.entry {
  padding: 1rem 1.25rem 1.25rem;

  &.latest {
    border-color: rgba(249, 115, 22, 0.45);
    box-shadow: 0 0 0 1px rgba(249, 115, 22, 0.18) inset;
  }
}

.entry-head {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  flex-wrap: wrap;
  margin-bottom: 0.6rem;
}

.version-badge {
  font-weight: 700;
  font-size: 0.95rem;
  color: var(--senpai-accent-deep);
  background: var(--senpai-accent-soft);
  border-radius: 8px;
  padding: 0.25rem 0.6rem;
}

.entry-title {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex: 1;
  min-width: 200px;

  .title-text {
    font-weight: 600;
    color: var(--senpai-text);
  }
}

.entry-date {
  font-size: 0.8rem;
  color: var(--senpai-muted);
  font-variant-numeric: tabular-nums;
}

.highlights {
  margin: 0 0 0.75rem;
  padding-left: 1.1rem;
  color: var(--senpai-text);
  font-size: 0.86rem;
  line-height: 1.65;
}

.pages-table {
  background: transparent;
  margin-bottom: 0.6rem;
}

.foot-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin-top: 0.35rem;
}

.foot-label {
  font-size: 0.72rem;
  color: var(--senpai-muted);
  letter-spacing: 0.04em;
}

.api-chip {
  font-size: 0.72rem;
  color: var(--senpai-accent-deep);
  background: var(--senpai-accent-soft);
  padding: 0.1rem 0.4rem;
  border-radius: 5px;
}

code {
  font-size: 0.85em;
  color: var(--senpai-accent-deep);
  background: var(--senpai-accent-soft);
  padding: 0.1rem 0.35rem;
  border-radius: 4px;
}
</style>
