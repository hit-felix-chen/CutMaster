import { defineConfig } from '@hey-api/openapi-ts'

export default defineConfig({
  input: process.env.CUTMASTER_OPENAPI_URL || 'http://127.0.0.1:8000/api/openapi.json',
  output: 'src/api/generated',
  plugins: ['@hey-api/client-fetch'],
})
