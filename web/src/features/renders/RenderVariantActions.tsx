import { Download, OctagonX, Play, RotateCcw, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  isRenderExecutionActive,
  renderRecoveryAction,
  type RenderRecoveryAction,
} from '@/features/renders/render-state'
import { api, type ExecutionSummary, type RenderVariant } from '@/features/shared/api'

export function RenderVariantActions({
  variant,
  execution,
  busy,
  onPlay,
  onRecover,
  onStop,
  onDelete,
}: {
  variant: RenderVariant
  execution: ExecutionSummary | null
  busy: boolean
  onPlay?: () => void
  onRecover: (action: RenderRecoveryAction) => void
  onStop: (attemptId: string) => void
  onDelete: () => void
}) {
  const { t } = useTranslation('common')
  const [deleteArmedFor, setDeleteArmedFor] = useState<string | null>(null)
  const deletionKey = `${variant.render_variant_id}:${variant.status}`
  const deleteArmed = deleteArmedFor === deletionKey
  const active = isRenderExecutionActive(variant, execution)
  const recovery = renderRecoveryAction(variant)

  useEffect(() => {
    if (!deleteArmedFor) return
    const timeout = window.setTimeout(() => setDeleteArmedFor(null), 6000)
    return () => window.clearTimeout(timeout)
  }, [deleteArmedFor])

  return (
    <div className="render-actions">
      {variant.status === 'ready' && onPlay ? (
        <button
          className="button button--secondary"
          type="button"
          disabled={busy}
          onClick={onPlay}
        >
          <Play size={14} aria-hidden="true" />
          {t('renders.play')}
        </button>
      ) : null}
      {variant.status === 'ready' ? (
        <a
          className="button button--secondary"
          href={
            variant.download_url ??
            api.renderVariants.downloadUrl(variant.render_variant_id)
          }
          download={`cutmaster-${variant.render_variant_id}.mp4`}
        >
          <Download size={14} aria-hidden="true" />
          {t('renders.downloadCopy')}
        </a>
      ) : null}
      {recovery ? (
        <button
          className="button button--secondary"
          type="button"
          disabled={busy}
          onClick={() => onRecover(recovery)}
        >
          <RotateCcw size={14} aria-hidden="true" />
          {t(`renders.${recovery}`)}
        </button>
      ) : null}
      {active && execution ? (
        <button
          className="button button--secondary"
          type="button"
          disabled={busy || execution.attempt.status === 'stopping'}
          onClick={() => onStop(execution.attempt.attempt_id)}
        >
          <OctagonX size={14} aria-hidden="true" />
          {execution.attempt.status === 'stopping'
            ? t('renders.stopping')
            : t('renders.stop')}
        </button>
      ) : null}
      {!active ? (
        <button
          className={`button ${deleteArmed ? 'button--danger' : 'button--secondary'}`}
          type="button"
          disabled={busy}
          onClick={() => {
            if (deleteArmed) onDelete()
            else setDeleteArmedFor(deletionKey)
          }}
        >
          <Trash2 size={14} aria-hidden="true" />
          {deleteArmed ? t('renders.confirmDelete') : t('renders.delete')}
        </button>
      ) : null}
    </div>
  )
}
