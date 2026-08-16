import { RotateCcw, ShieldAlert } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Navigate } from 'react-router-dom'

import { useApplicationHealth } from '@/app/use-application-health'
import { ErrorState, LoadingState } from '@/components/ui/AsyncState'

export function SetupPage() {
  const { t } = useTranslation('common')
  const health = useApplicationHealth()

  if (health.isPending) {
    return (
      <main className="standalone-page">
        <LoadingState />
      </main>
    )
  }
  if (health.isError) {
    return (
      <main className="standalone-page">
        <ErrorState onRetry={() => void health.refetch()} />
      </main>
    )
  }
  if (Object.values(health.data.configured).every(Boolean)) {
    return <Navigate to="/projects" replace />
  }

  const missing = Object.entries(health.data.configured)
    .filter(([, configured]) => !configured)
    .map(([provider]) => provider.toUpperCase())

  return (
    <main className="standalone-page">
      <section className="setup-panel">
        <span className="setup-panel__mark">M</span>
        <ShieldAlert size={26} />
        <h1>{t('setup.title')}</h1>
        <p>{t('setup.body')}</p>
        <p className="setup-panel__missing">
          {t('setup.missing', { providers: missing.join(', ') })}
        </p>
        <button
          className="button button--primary"
          type="button"
          onClick={() => void health.refetch()}
        >
          <RotateCcw size={16} />
          {t('setup.reload')}
        </button>
      </section>
    </main>
  )
}
