import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { resolve } from 'path'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: { '@': resolve(__dirname, 'src') },
  },
  server: {
    port: 41788,
    proxy: {
      '/x7k9-dl-senpai-console': {
        target: 'http://127.0.0.1:28473',
        changeOrigin: true,
      },
    },
  },
})
