import { useTranslation } from 'react-i18next'

interface ExecutionFailureProps {
  operationType?: string | null
  ownerType: string
  status: string
  className?: string
}

const operationKeys: Record<string, string> = {
  material_analysis: 'materialAnalysis',
  aster_planning: 'asterPlanning',
  rendering: 'rendering',
}

const ownerKeys: Record<string, string> = {
  material: 'materialAnalysis',
  run: 'asterPlanning',
  render_variant: 'rendering',
}

function failureState(status: string) {
  const normalized = status.trim().toLowerCase()
  if (normalized === 'interrupted') return 'interrupted'
  if (normalized === 'unavailable') return 'unavailable'
  return 'failed'
}

export function ExecutionFailure({
  operationType,
  ownerType,
  status,
  className,
}: ExecutionFailureProps) {
  const { t } = useTranslation('common')
  const operation =
    (operationType ? operationKeys[operationType] : undefined) ??
    ownerKeys[ownerType] ??
    'generic'
  const state = failureState(status)

  return (
    <p className={className} role="alert">
      {t(`executionFailures.${operation}.${state}`, {
        defaultValue: t(`executionFailures.generic.${state}`),
      })}
    </p>
  )
}
