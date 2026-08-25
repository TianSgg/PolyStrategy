import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// API_GATEWAY: Traefik 网关地址（开发默认 8000）
const GATEWAY = process.env.VITE_API_GATEWAY || 'http://localhost:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': GATEWAY,
      '/ws': {
        target: GATEWAY.replace('http', 'ws'),
        ws: true,
      },
      '/auth': GATEWAY,
    },
  },
})
