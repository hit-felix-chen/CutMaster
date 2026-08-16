import { fileURLToPath, URL } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv } from 'vite'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiOrigin = env.CUTMASTER_WEB_API_ORIGIN || 'http://127.0.0.1:8000'

  return {
    plugins: [react()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    build: {
      manifest: true,
      sourcemap: true,
    },
    server: {
      host: '127.0.0.1',
      port: 5173,
      proxy: {
        '/api': apiOrigin,
      },
    },
  }
})
