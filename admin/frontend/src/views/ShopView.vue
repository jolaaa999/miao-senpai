<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { consoleApi, type OutfitShopSnapshot, type ShopGroup } from '@/api/client'

const snap = ref<OutfitShopSnapshot>({ catalog: [], groups: [], thumb_dir: '', catalog_path: '' })
const loading = ref(false)
const keyword = ref('')
const selectedGroupId = ref('')
// 缩略图用 blob URL（接口带鉴权头，不能直接给 <img src> 走后端路径）
const thumbs = ref<Record<string, string>>({})

const catalog = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return snap.value.catalog
  return snap.value.catalog.filter(
    (it) => it.name.toLowerCase().includes(kw) || it.id.toLowerCase().includes(kw),
  )
})

const selectedGroup = computed<ShopGroup | undefined>(() =>
  snap.value.groups.find((g) => g.group_id === selectedGroupId.value),
)

// 目录里出现、但当前群货架没刷出来的衣服数量：用来判断货架是不是当天刷过
const slotCount = computed(() => selectedGroup.value?.slots.length ?? 0)

async function loadThumbs() {
  for (const item of snap.value.catalog) {
    if (!item.thumb || thumbs.value[item.id]) continue
    try {
      const res = await consoleApi.shopThumb(item.thumb)
      thumbs.value[item.id] = URL.createObjectURL(res.data)
    } catch {
      /* 缺图就给占位，页面不该因为一张图挂掉 */
    }
  }
}

async function load() {
  loading.value = true
  try {
    const res = await consoleApi.outfitShop()
    snap.value = res.data
    if (!selectedGroupId.value && snap.value.groups.length) {
      selectedGroupId.value = snap.value.groups[0].group_id
    }
    await loadThumbs()
  } finally {
    loading.value = false
  }
}

function revokeThumbs() {
  for (const url of Object.values(thumbs.value)) URL.revokeObjectURL(url)
  thumbs.value = {}
}

function groupLabel(g: ShopGroup) {
  return g.name ? `${g.name}（${g.group_id}）` : g.group_id
}

// 某件衣服今天出现在哪些群的货架上：买之前先看这个群刷没刷
function shelfGroups(itemId: string) {
  return snap.value.groups.filter((g) => g.slots.some((s) => s.id === itemId))
}

onMounted(load)
onUnmounted(revokeThumbs)
</script>

<template>
  <div>
    <h1 class="page-title">服装店</h1>
    <p class="page-subtitle">
      积分商店的换装货架：目录共 {{ snap.catalog.length }} 件，每天给每个群刷 4 格；
      学姐当前穿的是「当前穿着」，玩家买过的都记在群衣柜里。
      数据源 <code>data/dl_senpai/shop/</code>。
    </p>

    <div class="toolbar glass-card">
      <span>群：</span>
      <el-select v-model="selectedGroupId" style="width: 300px" placeholder="选择群">
        <el-option v-for="g in snap.groups" :key="g.group_id" :label="groupLabel(g)" :value="g.group_id" />
      </el-select>
      <el-input v-model="keyword" placeholder="按名称 / id 搜索服装" style="width: 220px" clearable />
      <el-button @click="load">刷新</el-button>
    </div>

    <div v-if="selectedGroup" class="group-panel glass-card">
      <div class="panel-head">
        <span class="panel-title">{{ selectedGroup.name || selectedGroup.group_id }} 今日货架</span>
        <span class="panel-meta">
          刷新日 {{ selectedGroup.slots_refresh_day || '—' }} ·
          {{ slotCount }} 格 ·
          {{ selectedGroup.warehouse_user_count }} 人买过 · 共 {{ selectedGroup.owned_count }} 件不同服装
        </span>
      </div>
      <div class="slot-row">
        <div v-for="slot in selectedGroup.slots" :key="slot.id" class="slot-card">
          <div class="slot-thumb">
            <img v-if="thumbs[slot.id]" :src="thumbs[slot.id]" :alt="slot.name" />
            <div v-else class="placeholder">👗</div>
          </div>
          <div class="slot-name">{{ slot.name }}</div>
          <div class="slot-id">{{ slot.id }}</div>
        </div>
      </div>
      <div class="current-row">
        <span class="label">当前穿着</span>
        <el-tag type="warning">{{ selectedGroup.current_outfit_name || '（未设置）' }}</el-tag>
        <span v-if="selectedGroup.current_outfit_id" class="muted">{{ selectedGroup.current_outfit_id }}</span>
      </div>
    </div>

    <h2 class="section-title">全部目录</h2>
    <div v-loading="loading" class="catalog-grid">
      <div v-for="item in catalog" :key="item.id" class="catalog-card glass-card">
        <div class="card-thumb">
          <img v-if="thumbs[item.id]" :src="thumbs[item.id]" :alt="item.name" />
          <div v-else class="placeholder">👗</div>
        </div>
        <div class="card-body">
          <div class="name-row">
            <span class="name">{{ item.name }}</span>
            <span class="price">{{ item.price }} 分</span>
          </div>
          <div class="id">{{ item.id }}</div>
          <p class="outfit-en">{{ item.outfit_en }}</p>
          <div v-if="shelfGroups(item.id).length" class="shelf-hint">
            <span class="hint-label">今日货架</span>
            <el-tag v-for="g in shelfGroups(item.id)" :key="g.group_id" size="small" effect="plain">
              {{ g.name || g.group_id }}
            </el-tag>
          </div>
        </div>
      </div>
      <div v-if="!catalog.length && !loading" class="empty glass-card">没有匹配的服装</div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.toolbar {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 1rem 1.25rem;
  margin-bottom: 1rem;
  flex-wrap: wrap;

  span {
    color: var(--senpai-muted);
  }
}

.group-panel {
  padding: 1rem 1.25rem 1.15rem;
  margin-bottom: 1.25rem;
}

.panel-head {
  display: flex;
  align-items: baseline;
  gap: 0.75rem;
  flex-wrap: wrap;
  margin-bottom: 0.75rem;

  .panel-title {
    font-weight: 600;
    color: var(--senpai-accent-deep);
  }

  .panel-meta {
    font-size: 0.76rem;
    color: var(--senpai-muted);
  }
}

.slot-row {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 0.75rem;
  margin-bottom: 0.75rem;
}

.slot-card {
  text-align: center;

  .slot-thumb {
    height: 110px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: linear-gradient(135deg, rgba(251, 146, 60, 0.12), rgba(56, 189, 248, 0.1));
    border-radius: 10px;
    overflow: hidden;

    img {
      max-width: 100%;
      max-height: 100%;
      object-fit: contain;
    }
  }

  .slot-name {
    font-size: 0.82rem;
    margin-top: 0.35rem;
    color: var(--senpai-text);
  }

  .slot-id {
    font-size: 0.68rem;
    color: var(--senpai-muted);
  }
}

.current-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.84rem;

  .label {
    color: var(--senpai-muted);
  }

  .muted {
    color: var(--senpai-muted);
    font-size: 0.76rem;
  }
}

.section-title {
  font-size: 1rem;
  margin: 0 0 0.75rem;
  color: var(--senpai-accent-deep);
}

.catalog-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 1rem;
  min-height: 120px;
}

.catalog-card {
  overflow: hidden;
  transition: transform 0.2s;

  &:hover {
    transform: translateY(-2px);
  }
}

.card-thumb {
  height: 160px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, rgba(251, 146, 60, 0.12), rgba(56, 189, 248, 0.1));
  overflow: hidden;

  img {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
  }
}

.placeholder {
  font-size: 2rem;
  opacity: 0.5;
}

.card-body {
  padding: 0.7rem 0.9rem 0.9rem;
}

.name-row {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 0.5rem;

  .name {
    font-weight: 600;
    color: var(--senpai-text);
  }

  .price {
    font-size: 0.78rem;
    color: var(--senpai-accent-deep);
  }
}

.id {
  font-size: 0.68rem;
  color: var(--senpai-muted);
  margin-bottom: 0.35rem;
}

.shelf-hint {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.3rem;
  margin-top: 0.4rem;

  .hint-label {
    font-size: 0.68rem;
    color: var(--senpai-muted);
  }
}

.outfit-en {
  font-size: 0.74rem;
  color: var(--senpai-muted);
  line-height: 1.5;
  margin: 0;
  max-height: 66px;
  overflow-y: auto;
}

.empty {
  grid-column: 1 / -1;
  text-align: center;
  padding: 2.5rem;
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
