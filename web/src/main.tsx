import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { App } from '@/app/App'
import { AppErrorBoundary } from '@/app/AppErrorBoundary'
import { AppProviders } from '@/app/AppProviders'
import '@/i18n'
import '@/styles/index.css'

const container = document.getElementById('root')

if (!container) {
  throw new Error('CutMaster Web root element was not found.')
}

createRoot(container).render(
  <StrictMode>
    <AppErrorBoundary>
      <AppProviders>
        <App />
      </AppProviders>
    </AppErrorBoundary>
  </StrictMode>,
)
