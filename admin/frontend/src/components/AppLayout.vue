<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { clearToken } from '@/api/client'
import {
  DataAnalysis,
  ChatDotRound,
  Calendar,
  Picture,
  PictureFilled,
  Setting,
  Document,
  User,
  Star,
  SwitchButton,
  Operation,
  ChatLineRound,
} from '@element-plus/icons-vue'

const route = useRoute()
const router = useRouter()

const navItems = [
  { path: '/', name: '总览', icon: DataAnalysis },
  { path: '/sessions', name: '聊天记录', icon: ChatDotRound },
  { path: '/checkin', name: '签到管理', icon: Calendar },
  { path: '/affection', name: '好感管理', icon: Star },
  { path: '/stickers', name: '表情库', icon: Picture },
  { path: '/draw-gallery', name: '生图库', icon: PictureFilled },
  { path: '/persona', name: '学姐自设', icon: User },
  { path: '/speak-styles', name: '语言风格', icon: ChatLineRound },
  { path: '/features', name: '功能开关', icon: Setting },
  { path: '/group-features', name: '群功能开关', icon: Operation },
  { path: '/logs', name: '运行日志', icon: Document },
]

const active = computed(() => route.path)

function logout() {
  clearToken()
  router.push('/login')
}
</script>

<template>
  <div class="layout">
    <aside class="sidebar glass-card">
      <div class="brand">
        <div class="brand-icon">🦊</div>
        <div>
          <div class="brand-title">学姐控制台</div>
          <div class="brand-sub">DL Senpai Console</div>
        </div>
      </div>
      <el-menu :default-active="active" router class="nav-menu">
        <el-menu-item v-for="item in navItems" :key="item.path" :index="item.path">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.name }}</span>
        </el-menu-item>
      </el-menu>
      <button class="logout-btn" @click="logout">
        <el-icon><SwitchButton /></el-icon>
        退出登录
      </button>
    </aside>
    <main class="main">
      <RouterView />
    </main>
  </div>
</template>

<style scoped lang="scss">
.layout {
  display: flex;
  min-height: 100vh;
  padding: 1rem;
  gap: 1rem;
}

.sidebar {
  width: 240px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  padding: 1.25rem 0.75rem;
}

.brand {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 0 0.75rem 1.25rem;
  border-bottom: 1px solid var(--senpai-border);
  margin-bottom: 0.5rem;
}

.brand-icon {
  font-size: 2rem;
  filter: drop-shadow(0 0 8px var(--senpai-accent-glow));
}

.brand-title {
  font-weight: 700;
  font-size: 1.1rem;
  color: var(--senpai-accent-deep);
}

.brand-sub {
  font-size: 0.7rem;
  color: var(--senpai-muted);
  letter-spacing: 0.05em;
}

.nav-menu {
  flex: 1;
  border: none;
}

.nav-menu .el-menu-item {
  border-radius: 10px;
  margin: 2px 0;
}

.nav-menu .el-menu-item.is-active {
  background: var(--senpai-accent-soft) !important;
  color: var(--senpai-accent-deep) !important;
  font-weight: 600;
}

.logout-btn {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  margin: 0.75rem;
  padding: 0.65rem 1rem;
  background: transparent;
  border: 1px solid var(--senpai-border);
  border-radius: 10px;
  color: var(--senpai-muted);
  cursor: pointer;
  font-family: inherit;
  font-size: 0.875rem;
  transition: all 0.2s;

  &:hover {
    color: #f87171;
    border-color: rgba(248, 113, 113, 0.4);
    background: rgba(248, 113, 113, 0.08);
  }
}

.main {
  flex: 1;
  min-width: 0;
  padding: 0.5rem 0.5rem 1rem;
}
</style>
