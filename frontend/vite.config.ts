import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发服务器把 API 请求转发到本机 FastAPI，避免在前端重复配置跨域。
export default defineConfig({
  plugins: [vue()],
  build: {
    rollupOptions: {
      output: {
        // 将图表运行时与其绘图内核分开缓存，避免单个入口脚本过大。
        manualChunks(id) {
          const normalizedId = id.replaceAll('\\', '/')
          if (normalizedId.includes('/node_modules/zrender/')) return 'zrender'
          if (normalizedId.includes('/node_modules/echarts/')) return 'echarts'
        },
      },
    },
  },
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
})
