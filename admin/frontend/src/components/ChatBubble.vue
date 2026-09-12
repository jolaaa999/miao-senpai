<script setup lang="ts">
defineProps<{
  role: 'user' | 'assistant'
  content: string
}>()
</script>

<template>
  <div class="bubble-row" :class="role">
    <div class="avatar">{{ role === 'assistant' ? '🦊' : '👤' }}</div>
    <div class="bubble glass-card">
      <div class="bubble-label">{{ role === 'assistant' ? '学姐' : '群友' }}</div>
      <div class="bubble-content">{{ content }}</div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.bubble-row {
  display: flex;
  gap: 0.75rem;
  margin-bottom: 1rem;

  &.assistant {
    flex-direction: row;
    .bubble {
      border-top-left-radius: 4px;
      border-color: rgba(249, 115, 22, 0.28);
      background: rgba(255, 247, 237, 0.9);
    }
    .bubble-label { color: var(--senpai-accent-deep); }
  }

  &.user {
    flex-direction: row-reverse;
    .bubble {
      border-top-right-radius: 4px;
      background: var(--senpai-sky-soft);
      border-color: rgba(56, 189, 248, 0.28);
    }
    .bubble-label { color: #0284c7; text-align: right; }
    .bubble-content { text-align: left; }
  }
}

.avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 1.1rem;
  background: rgba(249, 115, 22, 0.12);
  flex-shrink: 0;
}

.bubble {
  max-width: 75%;
  padding: 0.75rem 1rem;
}

.bubble-label {
  font-size: 0.7rem;
  font-weight: 600;
  margin-bottom: 0.35rem;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.bubble-content {
  font-size: 0.9rem;
  line-height: 1.55;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
