<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { authApi, setToken } from '@/api/client'
import { ElMessage } from 'element-plus'

const router = useRouter()
const username = ref('admin')
const password = ref('')
const loading = ref(false)

async function submit() {
  loading.value = true
  try {
    const { data } = await authApi.login(username.value, password.value)
    setToken(data.token)
    ElMessage.success('欢迎回来，学姐在等你～')
    router.push('/')
  } catch {
    ElMessage.error('用户名或密码不对哦')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-card glass-card">
      <div class="login-header">
        <span class="fox">🦊</span>
        <h1>学姐控制台</h1>
        <p>管理聊天记录 · 签到 · 表情库 · 功能状态</p>
      </div>
      <el-form @submit.prevent="submit">
        <el-form-item>
          <el-input v-model="username" placeholder="用户名" size="large" prefix-icon="User" />
        </el-form-item>
        <el-form-item>
          <el-input
            v-model="password"
            type="password"
            placeholder="密码"
            size="large"
            prefix-icon="Lock"
            show-password
            @keyup.enter="submit"
          />
        </el-form-item>
        <el-button type="primary" size="large" :loading="loading" class="submit-btn" @click="submit">
          进入控制台
        </el-button>
      </el-form>
      <p class="hint">默认账号见 <code>.env</code> 中 SENPAI_CONSOLE_* 配置</p>
    </div>
    <div class="deco deco-1" />
    <div class="deco deco-2" />
  </div>
</template>

<style scoped lang="scss">
.login-page {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  overflow: hidden;
}

.login-card {
  width: 100%;
  max-width: 400px;
  padding: 2.5rem 2rem;
  z-index: 1;
}

.login-header {
  text-align: center;
  margin-bottom: 2rem;

  .fox {
    font-size: 3rem;
    display: block;
    margin-bottom: 0.5rem;
    filter: drop-shadow(0 0 16px var(--senpai-accent-glow));
  }

  h1 {
    margin: 0;
    font-size: 1.75rem;
    background: linear-gradient(135deg, #ea580c, #fb923c, #38bdf8);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
  }

  p {
    color: var(--senpai-muted);
    font-size: 0.85rem;
    margin: 0.5rem 0 0;
  }
}

.submit-btn {
  width: 100%;
  height: 44px;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.hint {
  text-align: center;
  font-size: 0.75rem;
  color: var(--senpai-muted);
  margin-top: 1.5rem;

  code {
    color: var(--senpai-accent);
    background: var(--senpai-accent-soft);
    padding: 0.1rem 0.35rem;
    border-radius: 4px;
  }
}

.deco {
  position: absolute;
  border-radius: 50%;
  filter: blur(80px);
  pointer-events: none;
}

.deco-1 {
  width: 300px;
  height: 300px;
  background: rgba(251, 146, 60, 0.35);
  top: 10%;
  left: 15%;
}

.deco-2 {
  width: 250px;
  height: 250px;
  background: rgba(56, 189, 248, 0.28);
  bottom: 15%;
  right: 20%;
}
</style>
