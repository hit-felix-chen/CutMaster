import {
  AlertCircle,
  CheckCircle2,
  Clock3,
  LoaderCircle,
  PauseCircle,
  Recycle,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'

const normalizedStatus = (status: string) => status.trim().toLowerCase()

export function StatusBadge({ status }: { status: string }) {
  const { t } = useTranslation('common')
  const value = normalizedStatus(status)
  const tone =
    value === 'ready' || value === 'complete' || value === 'completed'
      ? 'success'
      : value === 'failed' || value === 'inconsistent' || value === 'unavailable'
        ? 'danger'
        : value === 'interrupted' || value === 'stopping'
          ? 'warning'
          : value === 'reused'
            ? 'visual'
            : 'info'
  const Icon =
    tone === 'success'
      ? CheckCircle2
      : tone === 'danger'
        ? AlertCircle
        : tone === 'warning'
          ? PauseCircle
          : value === 'queued'
            ? Clock3
            : value === 'reused'
              ? Recycle
              : LoaderCircle
  const labelKey: Record<string, string> = {
    ready: 'materials.ready',
    failed: 'activity.failed',
    inconsistent: 'materials.inconsistent',
    interrupted: 'activity.interrupted',
    complete: 'activity.complete',
    completed: 'activity.complete',
    reused: 'activity.reused',
    queued: 'activity.queued',
    analysing: 'materials.analysing',
    running: 'activity.running',
    rendering: 'renders.rendering',
    planners: 'activity.running',
    retrying: 'activity.retrying',
    stopping: 'activity.stopping',
    unavailable: 'common.unavailable',
  }
  const animated = [
    'analysing',
    'running',
    'rendering',
    'planners',
    'retrying',
  ].includes(value)
  return (
    <span className={`status-badge status-badge--${tone}`}>
      <Icon size={13} aria-hidden="true" className={animated ? 'spin' : undefined} />
      {labelKey[value] ? t(labelKey[value]) : status}
    </span>
  )
}
