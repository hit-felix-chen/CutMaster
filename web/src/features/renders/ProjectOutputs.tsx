import { ExternalLink, PlaySquare } from 'lucide-react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { Link, useParams, useSearchParams } from 'react-router-dom'

import { appRoutes } from '@/app/routes'
import { useEventStream } from '@/app/providers/event-stream-context'
import { ErrorState, LoadingState } from '@/components/ui/AsyncState'
import { OperationProblem } from '@/components/ui/OperationProblem'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { RenderVariantActions } from '@/features/renders/RenderVariantActions'
import { isRenderExecutionActive } from '@/features/renders/render-state'
import { api } from '@/features/shared/api'
import { ExecutionFailure } from '@/features/shared/ExecutionFailure'

function formatDuration(value: number | null) {
  if (value === null) return '—'
  const rounded = Math.max(0, Math.round(value))
  const minutes = Math.floor(rounded / 60)
  const seconds = rounded % 60
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

function formatBytes(value: number | null) {
  if (value === null) return '—'
  if (value < 1024) return `${value} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let amount = value / 1024
  let unit = units[0]
  for (let index = 1; index < units.length && amount >= 1024; index += 1) {
    amount /= 1024
    unit = units[index]
  }
  return `${amount >= 10 ? amount.toFixed(0) : amount.toFixed(1)} ${unit}`
}

export function ProjectOutputsView() {
  const { t } = useTranslation('common')
  const queryClient = useQueryClient()
  const { isConnected } = useEventStream()
  const { projectId = '' } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const variants = useQuery({
    queryKey: ['project-render-variants', projectId],
    queryFn: () => api.renderVariants.listForProject(projectId),
    enabled: Boolean(projectId),
    refetchInterval: (query) =>
      !isConnected &&
      query.state.data?.items?.some(({ render_variant: variant, execution }) =>
        isRenderExecutionActive(variant, execution),
      )
        ? 2000
        : false,
  })
  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: ['project-render-variants', projectId],
      }),
      queryClient.invalidateQueries({ queryKey: ['project-workspace', projectId] }),
      queryClient.invalidateQueries({ queryKey: ['render-variants', 'edit'] }),
      queryClient.invalidateQueries({ queryKey: ['activity'] }),
    ])
  }
  const recover = useMutation({
    mutationFn: ({
      variantId,
      action,
    }: {
      variantId: string
      action: 'retry' | 'resume' | 'renderAgain'
    }) => api.renderVariants[action](variantId),
    onSuccess: refresh,
  })
  const stop = useMutation({
    mutationFn: (attemptId: string) => api.attempts.stop(attemptId),
    onSuccess: refresh,
  })
  const remove = useMutation({
    mutationFn: (variantId: string) => api.renderVariants.delete(variantId),
    onSuccess: async (_, variantId) => {
      if (searchParams.get('variant') === variantId) {
        const next = new URLSearchParams(searchParams)
        next.delete('variant')
        setSearchParams(next, { replace: true })
      }
      await refresh()
    },
  })

  if (variants.isPending) return <LoadingState />
  if (variants.isError) {
    return <ErrorState onRetry={() => void variants.refetch()} />
  }

  const items = variants.data.items
  const requestedVariantId = searchParams.get('variant')
  const selected = items.find(
    ({ render_variant: variant }) => variant.render_variant_id === requestedVariantId,
  )
  const selectedReady = selected?.render_variant.status === 'ready' ? selected : null
  const busy = recover.isPending || stop.isPending || remove.isPending
  const error = recover.error ?? stop.error ?? remove.error

  return (
    <div className="project-section project-outputs">
      <header className="section-header">
        <div>
          <span className="eyebrow">Renderer</span>
          <h2>{t('projects.outputs')}</h2>
          <p>{t('renders.outputsHelp')}</p>
        </div>
      </header>
      {selectedReady ? (
        <section className="output-player" aria-label={t('renders.selectedOutput')}>
          {/* The rendered master may include original dialogue, but no caption sidecar is produced. */}
          {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
          <video
            controls
            autoPlay
            playsInline
            preload="metadata"
            src={
              selectedReady.render_variant.media_url ??
              api.renderVariants.mediaUrl(
                selectedReady.render_variant.render_variant_id,
              )
            }
            onError={() => void variants.refetch()}
          />
          <div>
            <strong>
              {t(
                `renders.audioMode.${selectedReady.render_variant.specification.audio_mode}`,
              )}
            </strong>
            <StatusBadge status={selectedReady.render_variant.status} />
          </div>
        </section>
      ) : null}
      {error ? (
        <div className="review-save-error" role="alert">
          <strong>{t('renders.operationFailed')}</strong>
          <OperationProblem error={error} />
        </div>
      ) : null}
      {items.length === 0 ? (
        <section className="empty-panel">
          <PlaySquare size={28} />
          <h3>{t('projects.noOutputs')}</h3>
          <p>{t('renders.noOutputsHelp')}</p>
        </section>
      ) : (
        <div className="output-list">
          {items.map((item, index) => {
            const variant = item.render_variant
            const reviewUrl = `${appRoutes.review(
              projectId,
              variant.run_id,
              variant.edit_id,
            )}?variant=${encodeURIComponent(variant.render_variant_id)}`
            return (
              <article
                className={`output-card${requestedVariantId === variant.render_variant_id ? ' is-active' : ''}`}
                key={variant.render_variant_id}
              >
                <header>
                  <div>
                    <span className="eyebrow">
                      {t('review.variantLabel', { number: index + 1 })}
                    </span>
                    <h3>
                      {t(`renders.audioMode.${variant.specification.audio_mode}`)}
                    </h3>
                  </div>
                  <StatusBadge status={variant.status} />
                </header>
                <dl className="output-card__facts">
                  <div>
                    <dt>{t('projects.runs')}</dt>
                    <dd>
                      {t('projects.runSequence', { sequence: variant.run_sequence })}
                    </dd>
                  </div>
                  <div>
                    <dt>{t('review.frozenEdit')}</dt>
                    <dd>
                      {t('review.editLabel', { sequence: variant.edit_sequence })}
                    </dd>
                  </div>
                  <div>
                    <dt>{t('renders.duration')}</dt>
                    <dd>{formatDuration(variant.duration_sec)}</dd>
                  </div>
                  <div>
                    <dt>{t('renders.videoFormat')}</dt>
                    <dd>
                      {variant.specification.renderer.width}×
                      {variant.specification.renderer.height} ·{' '}
                      {variant.specification.renderer.fps} fps ·{' '}
                      {variant.specification.renderer.encoder}
                    </dd>
                  </div>
                  <div>
                    <dt>{t('renders.fileSize')}</dt>
                    <dd>{formatBytes(variant.size_bytes)}</dd>
                  </div>
                </dl>
                {variant.failure_message ? (
                  <ExecutionFailure
                    className="output-card__failure"
                    operationType="rendering"
                    ownerType="render_variant"
                    status={variant.status}
                  />
                ) : null}
                <footer>
                  <RenderVariantActions
                    variant={variant}
                    execution={item.execution}
                    busy={busy}
                    onPlay={() => {
                      const next = new URLSearchParams(searchParams)
                      next.set('variant', variant.render_variant_id)
                      setSearchParams(next, { replace: true })
                    }}
                    onRecover={(action) =>
                      recover.mutate({
                        variantId: variant.render_variant_id,
                        action,
                      })
                    }
                    onStop={(attemptId) => stop.mutate(attemptId)}
                    onDelete={() => remove.mutate(variant.render_variant_id)}
                  />
                  <Link className="text-link" to={reviewUrl}>
                    {t('renders.openReview')}
                    <ExternalLink size={13} aria-hidden="true" />
                  </Link>
                </footer>
              </article>
            )
          })}
        </div>
      )}
    </div>
  )
}
