import { AlertTriangle, LoaderCircle, RotateCcw } from 'lucide-react'
import { useTranslation } from 'react-i18next'

interface ErrorStateProps {
  onRetry?: () => void
}

export function LoadingState() {
  const { t } = useTranslation('common')
  return (
    <div className="async-state" role="status">
      <LoaderCircle className="spin" aria-hidden="true" />
      <span>{t('common.loading')}</span>
    </div>
  )
}

export function ErrorState({ onRetry }: ErrorStateProps) {
  const { t } = useTranslation('common')
  return (
    <div className="async-state async-state--error" role="alert">
      <AlertTriangle aria-hidden="true" />
      <div>
        <strong>{t('common.errorTitle')}</strong>
        <p>{t('unexpectedError')}</p>
      </div>
      {onRetry ? (
        <button className="button button--secondary" type="button" onClick={onRetry}>
          <RotateCcw size={15} aria-hidden="true" />
          {t('common.retry')}
        </button>
      ) : null}
    </div>
  )
}
