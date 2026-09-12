import { WriteButton, WriteSelect } from '@/components/ui/WriteControls'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Check,
  ChevronLeft,
  Clapperboard,
  Film,
  Lock,
  Music2,
  Play,
  RotateCcw,
  Save,
  Undo2,
  Waves,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Link,
  useBlocker,
  useNavigate,
  useParams,
  useSearchParams,
} from 'react-router-dom'

import { appRoutes } from '@/app/routes'
import { useEventStream } from '@/app/providers/event-stream-context'
import { LoadingState } from '@/components/ui/AsyncState'
import { OperationProblem } from '@/components/ui/OperationProblem'
import { StatusBadge } from '@/components/ui/StatusBadge'
import {
  changesReviewContext,
  revisionReplacements,
  type RevisionDraft,
} from '@/features/review/review-draft'
import { RenderVariantActions } from '@/features/renders/RenderVariantActions'
import { isRenderExecutionActive } from '@/features/renders/render-state'
import {
  api,
  type FrozenEditReview,
  type RenderAudioMode,
  type ReviewCandidate,
  type ReviewRenderVariant,
  type ReviewSlot,
  type ReviewTrajectory,
} from '@/features/shared/api'
import { ExecutionFailure } from '@/features/shared/ExecutionFailure'

function padSequence(value: number) {
  return String(value).padStart(2, '0')
}

function formatSeconds(value: number) {
  const safe = Math.max(0, value)
  const minutes = Math.floor(safe / 60)
  const seconds = safe - minutes * 60
  return `${String(minutes).padStart(2, '0')}:${seconds.toFixed(1).padStart(4, '0')}`
}

function vttTimestamp(value: number) {
  const milliseconds = Math.max(0, Math.round(value * 1000))
  const hours = Math.floor(milliseconds / 3_600_000)
  const minutes = Math.floor((milliseconds % 3_600_000) / 60_000)
  const seconds = Math.floor((milliseconds % 60_000) / 1000)
  const remainder = milliseconds % 1000
  return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${String(remainder).padStart(3, '0')}`
}

function sourceDialogueCues(slots: ReviewSlot[]) {
  const cues: Array<{ start: number; end: number; text: string }> = []
  for (const slot of slots) {
    const items = slot.dialogue_anchor?.dialogue_items
    if (!Array.isArray(items)) continue
    for (const item of items) {
      if (typeof item !== 'object' || item === null) continue
      const value = item as Record<string, unknown>
      const range = value.time_range
      if (typeof range !== 'object' || range === null) continue
      const start = (range as Record<string, unknown>).start_sec
      const end = (range as Record<string, unknown>).end_sec
      const text = value.text
      const speaker = value.speaker
      if (
        typeof start !== 'number' ||
        typeof end !== 'number' ||
        typeof text !== 'string'
      )
        continue
      cues.push({
        start,
        end,
        text: typeof speaker === 'string' ? `${speaker}: ${text}` : text,
      })
    }
  }
  return cues
}

function captionTrack(data: FrozenEditReview, rendered: boolean) {
  const cues = rendered
    ? data.timeline.dialogue_cues.map((cue) => ({
        start: cue.start_sec,
        end: cue.end_sec,
        text: cue.speaker ? `${cue.speaker}: ${cue.text}` : cue.text,
      }))
    : sourceDialogueCues(data.slots)
  const body = cues
    .map(
      (cue, index) =>
        `${index + 1}\n${vttTimestamp(cue.start)} --> ${vttTimestamp(cue.end)}\n${cue.text.replaceAll('-->', '→')}\n`,
    )
    .join('\n')
  return `data:text/vtt;charset=utf-8,${encodeURIComponent(`WEBVTT\n\n${body}`)}`
}

function scoreLabel(value: number | null) {
  if (value === null || !Number.isFinite(value)) return '—'
  const normalized = value > 1 ? value / 5 : value
  return `${Math.round(Math.max(0, Math.min(1, normalized)) * 100)}%`
}

function selectedTrajectory(
  data: FrozenEditReview,
  slot: ReviewSlot,
  draft: RevisionDraft,
) {
  const trajectoryId = draft[slot.group_id] ?? slot.selected_trajectory_id
  return data.candidates[slot.group_id]?.find(
    (trajectory) => trajectory.trajectory_id === trajectoryId,
  )
}

function trajectoryCandidate(trajectory: ReviewTrajectory | undefined, slotId: string) {
  return trajectory?.items.find((candidate) => candidate.slot_id === slotId)
}

function CandidateCard({
  trajectory,
  candidate,
  active,
  original,
  disabled,
  onPreview,
  onChoose,
}: {
  trajectory: ReviewTrajectory
  candidate: ReviewCandidate
  active: boolean
  original: boolean
  disabled: boolean
  onPreview: () => void
  onChoose: () => void
}) {
  const { t } = useTranslation('common')
  return (
    <article className={`review-candidate${active ? ' is-active' : ''}`}>
      <header>
        <div>
          <strong>{trajectory.trajectory_id}</strong>
          <span>{candidate.source_timestamp}</span>
        </div>
        {active ? <Check size={16} aria-label={t('review.currentChoice')} /> : null}
      </header>
      <p>{candidate.description}</p>
      <dl className="review-candidate__scores">
        <div>
          <dt>{t('review.visualScore')}</dt>
          <dd>{scoreLabel(candidate.visual_score)}</dd>
        </div>
        <div>
          <dt>{t('review.semanticScore')}</dt>
          <dd>{scoreLabel(candidate.semantic_relevance)}</dd>
        </div>
        <div>
          <dt>{t('review.emotionScore')}</dt>
          <dd>{scoreLabel(candidate.emotional_intensity)}</dd>
        </div>
      </dl>
      {candidate.visual_evidence ? (
        <p className="review-candidate__evidence">{candidate.visual_evidence}</p>
      ) : null}
      <footer>
        <button className="button button--secondary" type="button" onClick={onPreview}>
          <Play size={14} aria-hidden="true" />
          {t('review.previewSource')}
        </button>
        {!original || !active ? (
          <WriteButton
            className="button button--primary"
            type="button"
            disabled={disabled || active || !trajectory.eligible_for_replacement}
            onClick={onChoose}
          >
            {t('review.useTrajectory')}
          </WriteButton>
        ) : null}
      </footer>
    </article>
  )
}

function SlotInspector({
  data,
  slot,
  draft,
  previewTrajectoryId,
  onPreview,
  onChoose,
  onUndo,
}: {
  data: FrozenEditReview
  slot: ReviewSlot
  draft: RevisionDraft
  previewTrajectoryId: string | null
  onPreview: (trajectory: ReviewTrajectory) => void
  onChoose: (trajectory: ReviewTrajectory) => void
  onUndo: () => void
}) {
  const { t } = useTranslation('common')
  const trajectories = data.candidates[slot.group_id] ?? []
  const currentId = draft[slot.group_id] ?? slot.selected_trajectory_id
  const dialogueText =
    slot.dialogue_anchor && typeof slot.dialogue_anchor.text === 'string'
      ? slot.dialogue_anchor.text
      : null
  return (
    <aside className="review-inspector" aria-label={t('review.inspector')}>
      <header className="review-inspector__header">
        <div>
          <span className="eyebrow">
            {t('review.slotNumber', { number: padSequence(slot.position) })}
          </span>
          <h2>{slot.picture}</h2>
        </div>
        {slot.is_anchor ? (
          <span className="review-anchor-badge">
            <Lock size={13} aria-hidden="true" />
            {t('review.storyAnchor')}
          </span>
        ) : null}
      </header>
      <dl className="review-slot-facts">
        <div>
          <dt>{t('review.outputRange')}</dt>
          <dd>
            {formatSeconds(slot.output_start_sec)}–{formatSeconds(slot.output_end_sec)}
          </dd>
        </div>
        <div>
          <dt>{t('review.sourceRange')}</dt>
          <dd>{slot.source_timestamp}</dd>
        </div>
      </dl>
      {dialogueText ? (
        <blockquote className="review-dialogue-quote">“{dialogueText}”</blockquote>
      ) : null}
      <section className="review-candidate-space">
        <header>
          <div>
            <h3>{t('review.candidateSpace')}</h3>
            <p>{t('review.trajectoryCount', { count: trajectories.length })}</p>
          </div>
          {draft[slot.group_id] ? (
            <WriteButton
              className="button button--secondary"
              type="button"
              onClick={onUndo}
            >
              <Undo2 size={14} aria-hidden="true" />
              {t('review.undoGroup')}
            </WriteButton>
          ) : null}
        </header>
        {slot.is_anchor ? (
          <div className="review-notice review-notice--locked">
            <Lock size={16} aria-hidden="true" />
            <p>{t('review.anchorLocked')}</p>
          </div>
        ) : null}
        <div className="review-candidate-list">
          {trajectories.map((trajectory) => {
            const candidate = trajectoryCandidate(trajectory, slot.slot_id)
            return candidate ? (
              <CandidateCard
                key={trajectory.trajectory_id}
                trajectory={trajectory}
                candidate={candidate}
                active={trajectory.trajectory_id === currentId}
                original={trajectory.trajectory_id === slot.selected_trajectory_id}
                disabled={slot.is_anchor}
                onPreview={() => onPreview(trajectory)}
                onChoose={() => onChoose(trajectory)}
              />
            ) : null
          })}
        </div>
        {previewTrajectoryId ? (
          <span className="review-previewing">
            {t('review.previewingTrajectory', { trajectory: previewTrajectoryId })}
          </span>
        ) : null}
      </section>
    </aside>
  )
}

function ReviewPlayer({
  data,
  slot,
  candidate,
  variant,
  previewCandidate,
  nextSlotId,
  onAdvanceSlot,
  onMediaError,
}: {
  data: FrozenEditReview
  slot: ReviewSlot
  candidate: ReviewCandidate | undefined
  variant: ReviewRenderVariant | undefined
  previewCandidate: ReviewCandidate | undefined
  nextSlotId: string | null
  onAdvanceSlot: (slotId: string) => void
  onMediaError: () => void
}) {
  const { t } = useTranslation('common')
  const player = useRef<HTMLVideoElement>(null)
  const advancing = useRef(false)
  const resumeAfterSeek = useRef(false)
  const sourceCandidate = previewCandidate ?? candidate
  const variantMediaUrl =
    variant?.status === 'ready'
      ? (variant.media_url ?? api.renderVariants.mediaUrl(variant.render_variant_id))
      : null
  const useVariant = Boolean(variantMediaUrl && !previewCandidate)
  const src = useVariant
    ? (variantMediaUrl ?? '')
    : sourceCandidate?.media_url || data.media.video.source_url
  const start = useVariant
    ? slot.output_start_sec
    : (sourceCandidate?.source_start_sec ?? slot.source_start_sec)
  const end = useVariant
    ? slot.output_end_sec
    : (sourceCandidate?.source_end_sec ?? slot.source_end_sec)
  const captions = captionTrack(data, useVariant)

  useEffect(() => {
    const video = player.current
    if (!video || !src) return
    const seek = () => {
      if (Number.isFinite(start)) video.currentTime = start
      advancing.current = false
      if (resumeAfterSeek.current) {
        resumeAfterSeek.current = false
        void video.play().catch(() => undefined)
      }
    }
    if (video.readyState >= 1) seek()
    else video.addEventListener('loadedmetadata', seek, { once: true })
    return () => video.removeEventListener('loadedmetadata', seek)
  }, [src, start])

  if (variant && variant.status !== 'ready' && !previewCandidate) {
    return (
      <section className="review-player review-player--empty">
        <Film size={32} aria-hidden="true" />
        <h2>{t('review.variantUnavailable')}</h2>
        <StatusBadge status={variant.status} />
        {variant.failure_message ? (
          <ExecutionFailure
            operationType="rendering"
            ownerType="render_variant"
            status={variant.status}
          />
        ) : null}
      </section>
    )
  }

  return (
    <section className="review-player">
      <video
        key={src}
        ref={player}
        src={src}
        controls
        playsInline
        preload="metadata"
        onError={onMediaError}
        onTimeUpdate={(event) => {
          if (event.currentTarget.currentTime < end || advancing.current) return
          if (!useVariant && !previewCandidate && nextSlotId) {
            advancing.current = true
            resumeAfterSeek.current = true
            onAdvanceSlot(nextSlotId)
          } else if (!useVariant) {
            event.currentTarget.pause()
          }
        }}
      >
        <track
          default
          kind="captions"
          src={captions}
          srcLang="en"
          label={t('review.dialogue')}
        />
      </video>
      <div className="review-player__hud">
        <span>
          {useVariant
            ? t('review.renderVariant')
            : previewCandidate
              ? t('review.sourcePreview')
              : t('review.sourceSequencePreview')}
        </span>
        <strong>
          {t('review.slotNumber', { number: padSequence(slot.position) })} ·{' '}
          {formatSeconds(start)}–{formatSeconds(end)}
        </strong>
      </div>
    </section>
  )
}

function ReviewTimeline({
  data,
  selectedSlotId,
  draft,
  onSelectSlot,
}: {
  data: FrozenEditReview
  selectedSlotId: string
  draft: RevisionDraft
  onSelectSlot: (slotId: string) => void
}) {
  const { t } = useTranslation('common')
  const duration = Math.max(data.plan.duration_sec, 0.001)
  const timelineWidth = Math.max(920, data.slots.length * 76)
  return (
    <section className="review-timeline" aria-label={t('review.timeline')}>
      <header>
        <div>
          <h2>{t('review.slotTimeline')}</h2>
          <p>{t('review.timelineHelp')}</p>
        </div>
        <span>{formatSeconds(data.plan.duration_sec)}</span>
      </header>
      <div className="review-timeline__scroll">
        <div className="review-timeline__canvas" style={{ minWidth: timelineWidth }}>
          <div className="review-slot-lane">
            {data.slots.map((slot) => {
              const width =
                ((slot.output_end_sec - slot.output_start_sec) / duration) * 100
              return (
                <button
                  key={slot.slot_id}
                  className={`review-slot${selectedSlotId === slot.slot_id ? ' is-active' : ''}${draft[slot.group_id] ? ' is-dirty' : ''}`}
                  type="button"
                  style={{ width: `${width}%` }}
                  onClick={() => onSelectSlot(slot.slot_id)}
                  aria-pressed={selectedSlotId === slot.slot_id}
                >
                  <span>{padSequence(slot.position)}</span>
                  {slot.is_anchor ? (
                    <Lock size={11} aria-label={t('review.storyAnchor')} />
                  ) : null}
                  {draft[slot.group_id] ? (
                    <span
                      className="review-slot__dirty"
                      aria-label={t('common.unsaved')}
                    />
                  ) : null}
                </button>
              )
            })}
          </div>
          <div className="review-audio-lane review-audio-lane--dialogue">
            <span className="review-audio-lane__label">
              <Waves size={14} aria-hidden="true" />
              {t('review.dialogue')}
            </span>
            <div className="review-audio-lane__track">
              {data.timeline.dialogue_cues.map((cue, index) => (
                <span
                  key={`${cue.slot_id}-${cue.start_sec}-${index}`}
                  className="review-dialogue-cue"
                  style={{
                    left: `${(cue.start_sec / duration) * 100}%`,
                    width: `${((cue.end_sec - cue.start_sec) / duration) * 100}%`,
                  }}
                  title={`${cue.speaker ? `${cue.speaker}: ` : ''}${cue.text}`}
                />
              ))}
            </div>
          </div>
          <div className="review-audio-lane review-audio-lane--music">
            <span className="review-audio-lane__label">
              <Music2 size={14} aria-hidden="true" />
              {t('review.musicBeats')}
            </span>
            <div className="review-audio-lane__track">
              {data.timeline.music_beats_available ? (
                data.timeline.music_beats_sec.map((beat, index) => (
                  <span
                    key={`${beat}-${index}`}
                    className="review-music-beat"
                    style={{ left: `${(beat / duration) * 100}%` }}
                  />
                ))
              ) : (
                <span className="review-audio-lane__empty">
                  {t('review.musicBeatsUnavailable')}
                </span>
              )}
            </div>
          </div>
        </div>
      </div>
    </section>
  )
}

function UnsavedChangesDialog({
  onStay,
  onDiscard,
}: {
  onStay: () => void
  onDiscard: () => void
}) {
  const { t } = useTranslation('common')
  const stayButton = useRef<HTMLButtonElement>(null)
  const discardButton = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    const previousFocus = document.activeElement
    stayButton.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onStay()
        return
      }
      if (event.key !== 'Tab') return
      const first = stayButton.current
      const last = discardButton.current
      if (!first || !last) return
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('keydown', onKeyDown)
      if (previousFocus instanceof HTMLElement) previousFocus.focus()
    }
  }, [onStay])

  return (
    <div className="dialog-layer">
      <div
        className="dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="review-leave-title"
        aria-describedby="review-leave-body"
      >
        <header>
          <h2 id="review-leave-title">{t('review.leaveTitle')}</h2>
        </header>
        <p id="review-leave-body">{t('review.leaveBody')}</p>
        <footer>
          <button
            ref={stayButton}
            className="button button--secondary"
            type="button"
            onClick={onStay}
          >
            {t('review.stayOnPage')}
          </button>
          <button
            ref={discardButton}
            className="button button--primary"
            type="button"
            onClick={onDiscard}
          >
            {t('review.discardAndLeave')}
          </button>
        </footer>
      </div>
    </div>
  )
}

export function ReviewWorkspace() {
  const { t } = useTranslation('common')
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { isConnected } = useEventStream()
  const { projectId = '', runId = '', editId = '' } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const [draft, setDraft] = useState<RevisionDraft>({})
  const [previewTrajectoryId, setPreviewTrajectoryId] = useState<string | null>(null)
  const updateSelection = (key: 'slot' | 'variant', value: string | null) => {
    const next = new URLSearchParams(searchParams)
    if (value) next.set(key, value)
    else next.delete(key)
    setSearchParams(next, { replace: true })
  }
  const dirty = Object.keys(draft).length > 0
  const dirtyRef = useRef(dirty)
  useEffect(() => {
    dirtyRef.current = dirty
  }, [dirty])
  const blocker = useBlocker(({ currentLocation, nextLocation }) =>
    dirtyRef.current ? changesReviewContext(currentLocation, nextLocation) : false,
  )
  const review = useQuery({
    queryKey: ['frozen-edit-review', editId],
    queryFn: () => api.frozenEdits.review(editId),
    enabled: Boolean(editId),
  })
  const renderVariants = useQuery({
    queryKey: ['render-variants', 'edit', editId],
    queryFn: () => api.renderVariants.listForEdit(editId),
    enabled: Boolean(editId),
    refetchInterval: (query) =>
      !isConnected &&
      query.state.data?.items?.some(({ render_variant: variant, execution }) =>
        isRenderExecutionActive(variant, execution),
      )
        ? 2000
        : false,
  })

  const refreshRenders = async () => {
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: ['render-variants', 'edit', editId],
      }),
      queryClient.invalidateQueries({ queryKey: ['frozen-edit-review', editId] }),
      queryClient.invalidateQueries({
        queryKey: ['project-render-variants', projectId],
      }),
      queryClient.invalidateQueries({ queryKey: ['project-workspace', projectId] }),
      queryClient.invalidateQueries({ queryKey: ['activity'] }),
    ])
  }
  const createRender = useMutation({
    mutationFn: (audioMode: RenderAudioMode) =>
      api.renderVariants.create(editId, audioMode),
    onSuccess: async ({ render_variant: variant }) => {
      setPreviewTrajectoryId(null)
      updateSelection('variant', variant.render_variant_id)
      await refreshRenders()
    },
  })
  const recoverRender = useMutation({
    mutationFn: ({
      variantId,
      action,
    }: {
      variantId: string
      action: 'retry' | 'resume' | 'renderAgain'
    }) => api.renderVariants[action](variantId),
    onSuccess: refreshRenders,
  })
  const stopRender = useMutation({
    mutationFn: (attemptId: string) => api.attempts.stop(attemptId),
    onSuccess: refreshRenders,
  })
  const deleteRender = useMutation({
    mutationFn: (variantId: string) => api.renderVariants.delete(variantId),
    onSuccess: async (_, variantId) => {
      if (searchParams.get('variant') === variantId) {
        updateSelection('variant', null)
      }
      await refreshRenders()
    },
  })

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (dirtyRef.current) event.preventDefault()
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [])

  const save = useMutation({
    mutationFn: () =>
      api.frozenEdits.createRevision(editId, revisionReplacements(draft)),
    onSuccess: async (result) => {
      dirtyRef.current = false
      setDraft({})
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['run', runId] }),
        queryClient.invalidateQueries({ queryKey: ['project-runs', projectId] }),
      ])
      navigate(
        result.review_url ||
          appRoutes.review(projectId, runId, result.frozen_edit.edit_id),
      )
    },
  })

  if (review.isPending) return <LoadingState />
  if (review.isError) {
    return (
      <div className="async-state async-state--error">
        <OperationProblem error={review.error} />
        <button
          className="button button--secondary"
          type="button"
          onClick={() => void review.refetch()}
        >
          {t('common.retry')}
        </button>
      </div>
    )
  }

  const data = review.data
  const requestedSlotId = searchParams.get('slot')
  const selectedSlot =
    data.slots.find((slot) => slot.slot_id === requestedSlotId) ?? data.slots[0]
  if (!selectedSlot) {
    return (
      <section className="empty-panel">
        <Film size={28} aria-hidden="true" />
        <h2>{t('review.noSlots')}</h2>
      </section>
    )
  }
  const requestedVariantId = searchParams.get('variant')
  const variantItems =
    renderVariants.data?.items ??
    data.variants.map((render_variant) => ({
      render_variant,
      execution: null,
    }))
  const selectedVariantItem = variantItems.find(
    ({ render_variant: variant }) => variant.render_variant_id === requestedVariantId,
  )
  const selectedVariantNumber = selectedVariantItem
    ? variantItems.indexOf(selectedVariantItem) + 1
    : null
  const selectedVariant = selectedVariantItem?.render_variant
  const currentTrajectory = selectedTrajectory(data, selectedSlot, draft)
  const currentCandidate = trajectoryCandidate(currentTrajectory, selectedSlot.slot_id)
  const selectedSlotIndex = data.slots.findIndex(
    (slot) => slot.slot_id === selectedSlot.slot_id,
  )
  const nextSlotId = data.slots[selectedSlotIndex + 1]?.slot_id ?? null
  const previewCandidate = trajectoryCandidate(
    previewTrajectoryId
      ? data.candidates[selectedSlot.group_id]?.find(
          (trajectory) => trajectory.trajectory_id === previewTrajectoryId,
        )
      : undefined,
    selectedSlot.slot_id,
  )
  const renderOperation =
    createRender.isPending ||
    recoverRender.isPending ||
    stopRender.isPending ||
    deleteRender.isPending
  const renderError =
    createRender.error ??
    recoverRender.error ??
    stopRender.error ??
    deleteRender.error ??
    renderVariants.error

  return (
    <div className="project-section project-section--review">
      <header className="review-toolbar">
        <Link
          className="review-toolbar__back"
          to={appRoutes.runDetail(projectId, runId)}
        >
          <ChevronLeft size={16} aria-hidden="true" />
          {t('review.backToRun')}
        </Link>
        <div className="review-toolbar__selectors">
          <label>
            <span>{t('review.frozenEdit')}</span>
            <WriteSelect
              aria-label={t('review.frozenEdit')}
              value={data.edit.edit_id}
              onChange={(event) =>
                navigate(appRoutes.review(projectId, runId, event.target.value))
              }
            >
              {data.versions.map((version) => (
                <option key={version.edit_id} value={version.edit_id}>
                  {t('review.editLabel', { sequence: padSequence(version.sequence) })}
                  {version.parent_edit_id
                    ? ` · ${t('review.basedOn', {
                        sequence: padSequence(
                          data.versions.find(
                            (item) => item.edit_id === version.parent_edit_id,
                          )?.sequence ?? 0,
                        ),
                      })}`
                    : ''}
                </option>
              ))}
            </WriteSelect>
          </label>
          <label>
            <span>{t('review.renderVariant')}</span>
            <WriteSelect
              aria-label={t('review.renderVariant')}
              value={selectedVariant?.render_variant_id ?? ''}
              onChange={(event) => {
                setPreviewTrajectoryId(null)
                updateSelection('variant', event.target.value || null)
              }}
            >
              <option value="">{t('review.sourceReview')}</option>
              {variantItems.map(({ render_variant: variant }, index) => (
                <option
                  key={variant.render_variant_id}
                  value={variant.render_variant_id}
                >
                  {t('review.variantLabel', { number: index + 1 })} ·{' '}
                  {t(`renders.audioMode.${variant.specification.audio_mode}`)} ·{' '}
                  {t(`renders.status.${variant.status}`)}
                </option>
              ))}
            </WriteSelect>
          </label>
        </div>
        <div className="review-toolbar__actions">
          {dirty ? <span className="dirty-label">{t('common.unsaved')}</span> : null}
          <WriteButton
            className="button button--secondary"
            type="button"
            disabled={!dirty || save.isPending}
            onClick={() => {
              setDraft({})
              setPreviewTrajectoryId(null)
            }}
          >
            <RotateCcw size={15} aria-hidden="true" />
            {t('common.undoChanges')}
          </WriteButton>
          <WriteButton
            className="button button--primary"
            type="button"
            disabled={!dirty || save.isPending}
            onClick={() => save.mutate()}
          >
            <Save size={15} aria-hidden="true" />
            {save.isPending ? t('review.savingRevision') : t('common.save')}
          </WriteButton>
        </div>
      </header>

      <section className="review-render-console" aria-label={t('renders.title')}>
        <header>
          <div>
            <span className="eyebrow">Renderer</span>
            <h2>
              {t('renders.targetEdit', { sequence: padSequence(data.edit.sequence) })}
            </h2>
            <p>{t('renders.targetHelp')}</p>
          </div>
          <div className="review-render-console__create">
            <WriteButton
              className="button button--secondary"
              type="button"
              disabled={dirty || renderOperation}
              onClick={() => createRender.mutate('dialogue')}
            >
              <Clapperboard size={15} aria-hidden="true" />
              {t('renders.renderDialoguePreview')}
            </WriteButton>
            <WriteButton
              className="button button--secondary"
              type="button"
              disabled={dirty || renderOperation}
              onClick={() => createRender.mutate('bgm_only')}
            >
              <Music2 size={15} aria-hidden="true" />
              {t('renders.createBgmOnly')}
            </WriteButton>
          </div>
        </header>
        {dirty ? <p className="render-hint">{t('renders.saveRevisionFirst')}</p> : null}
        {renderVariants.isPending ? (
          <p className="render-hint">{t('renders.loadingVariants')}</p>
        ) : null}
        {selectedVariantItem ? (
          <div className="review-render-console__selected">
            <div>
              <strong>
                {t(
                  `renders.audioMode.${selectedVariantItem.render_variant.specification.audio_mode}`,
                )}
              </strong>
              <span>{t('review.variantLabel', { number: selectedVariantNumber })}</span>
              <StatusBadge status={selectedVariantItem.render_variant.status} />
            </div>
            <RenderVariantActions
              variant={selectedVariantItem.render_variant}
              execution={selectedVariantItem.execution}
              busy={renderOperation}
              onRecover={(action) =>
                recoverRender.mutate({
                  variantId: selectedVariantItem.render_variant.render_variant_id,
                  action,
                })
              }
              onStop={(attemptId) => stopRender.mutate(attemptId)}
              onDelete={() =>
                deleteRender.mutate(
                  selectedVariantItem.render_variant.render_variant_id,
                )
              }
            />
          </div>
        ) : null}
        {renderError ? (
          <div className="review-save-error" role="alert">
            <strong>{t('renders.operationFailed')}</strong>
            <OperationProblem error={renderError} />
          </div>
        ) : null}
      </section>

      {save.isError ? (
        <div className="review-save-error" role="alert">
          <strong>{t('review.saveFailed')}</strong>
          <OperationProblem error={save.error} />
        </div>
      ) : null}

      <div className="review-workspace-grid">
        <SlotInspector
          data={data}
          slot={selectedSlot}
          draft={draft}
          previewTrajectoryId={previewTrajectoryId}
          onPreview={(trajectory) => setPreviewTrajectoryId(trajectory.trajectory_id)}
          onChoose={(trajectory) => {
            setPreviewTrajectoryId(null)
            setDraft((current) => {
              if (trajectory.trajectory_id === selectedSlot.selected_trajectory_id) {
                const next = { ...current }
                delete next[selectedSlot.group_id]
                return next
              }
              return {
                ...current,
                [selectedSlot.group_id]: trajectory.trajectory_id,
              }
            })
          }}
          onUndo={() => {
            setPreviewTrajectoryId(null)
            setDraft((current) => {
              const next = { ...current }
              delete next[selectedSlot.group_id]
              return next
            })
          }}
        />
        <ReviewPlayer
          data={data}
          slot={selectedSlot}
          candidate={currentCandidate}
          variant={selectedVariant}
          previewCandidate={previewCandidate}
          nextSlotId={nextSlotId}
          onAdvanceSlot={(slotId) => updateSelection('slot', slotId)}
          onMediaError={() => void renderVariants.refetch()}
        />
      </div>
      <ReviewTimeline
        data={data}
        selectedSlotId={selectedSlot.slot_id}
        draft={draft}
        onSelectSlot={(slotId) => {
          setPreviewTrajectoryId(null)
          updateSelection('slot', slotId)
        }}
      />

      {blocker.state === 'blocked' ? (
        <UnsavedChangesDialog
          onStay={() => blocker.reset()}
          onDiscard={() => {
            dirtyRef.current = false
            setDraft({})
            blocker.proceed()
          }}
        />
      ) : null}
    </div>
  )
}
