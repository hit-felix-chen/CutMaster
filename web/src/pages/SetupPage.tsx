import { useQuery } from '@tanstack/react-query'
import { RotateCcw, ShieldAlert } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Navigate, useLocation } from 'react-router-dom'

import { setupReturnTarget } from '@/app/setup-return'
import { useApplicationHealth } from '@/app/use-application-health'
import { LoadingState } from '@/components/ui/AsyncState'
import { OperationProblem } from '@/components/ui/OperationProblem'
import { api, type ProviderCapability } from '@/features/shared/api'
import { ProviderSettingsEditor } from '@/features/settings/ProviderSettingsEditor'

const CAPABILITIES: ProviderCapability[] = ['llm', 'vlm', 'asr']

export function SetupPage() {
  const { t } = useTranslation('common')
  const location = useLocation()
  const returnTo = setupReturnTarget(location.state)
  const health = useApplicationHealth()
  const ready = health.data
    ? CAPABILITIES.every((capability) => health.data.configured[capability])
    : false
  const settings = useQuery({
    queryKey: ['settings'],
    queryFn: api.settings.get,
    enabled: health.isSuccess && !ready,
  })

  if (health.isPending) {
    return (
      <main className="standalone-page">
        <LoadingState />
      </main>
    )
  }
  if (health.isError) {
    return <SetupProblem error={health.error} onRetry={() => void health.refetch()} />
  }
  if (ready) {
    return <Navigate to={returnTo} replace />
  }

  const missing = CAPABILITIES.filter(
    (capability) => !health.data.configured[capability],
  )
  return (
    <main className="standalone-page standalone-page--setup">
      <div className="setup-onboarding">
        <header className="setup-onboarding__header">
          <span className="setup-panel__mark">M</span>
          <div>
            <span className="eyebrow">MASTER</span>
            <h1>{t('setup.title')}</h1>
            <p>{t('setup.body')}</p>
          </div>
          <ShieldAlert size={28} aria-hidden="true" />
        </header>
        <div className="setup-onboarding__status" aria-label={t('setup.status')}>
          {CAPABILITIES.map((capability) => {
            const configured = health.data.configured[capability]
            return (
              <span className={configured ? 'configured' : 'missing'} key={capability}>
                {t(`settings.${capability}`)} ·{' '}
                {t(configured ? 'settings.configured' : 'settings.missing')}
              </span>
            )
          })}
        </div>
        <p className="setup-onboarding__missing">
          {t('setup.missing', {
            providers: missing.map((item) => item.toUpperCase()).join(', '),
          })}
        </p>
        {settings.isPending ? <LoadingState /> : null}
        {settings.isError ? (
          <section className="setup-onboarding__problem">
            <OperationProblem error={settings.error} />
            <button
              className="button button--secondary"
              type="button"
              onClick={() => void settings.refetch()}
            >
              <RotateCcw size={15} aria-hidden="true" />
              {t('common.retry')}
            </button>
          </section>
        ) : null}
        {settings.data ? (
          <ProviderSettingsEditor
            settings={settings.data}
            setupMode
            onSaved={async () => {
              await health.refetch()
            }}
          />
        ) : null}
      </div>
    </main>
  )
}

function SetupProblem({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  const { t } = useTranslation('common')
  return (
    <main className="standalone-page">
      <section className="setup-panel">
        <span className="setup-panel__mark">M</span>
        <h1>{t('setup.unavailableTitle')}</h1>
        <OperationProblem error={error} />
        <button className="button button--secondary" type="button" onClick={onRetry}>
          <RotateCcw size={15} aria-hidden="true" />
          {t('common.retry')}
        </button>
      </section>
    </main>
  )
}
