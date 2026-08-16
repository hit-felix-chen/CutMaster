import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
} from '@tanstack/react-query'
import {
  AlertTriangle,
  AudioWaveform,
  ChevronRight,
  Clock3,
  FileAudio2,
  Film,
  Info,
  LoaderCircle,
  OctagonX,
  RefreshCcw,
  Search,
  SlidersHorizontal,
  Sparkles,
  Trash2,
  UploadCloud,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Link,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from 'react-router-dom'

import { appRoutes } from '@/app/routes'
import { useEventStream } from '@/app/providers/event-stream-context'
import { ErrorState, LoadingState } from '@/components/ui/AsyncState'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { MaterialImportDialog } from '@/features/materials/MaterialImportDialog'
import { MemoryTabView } from '@/features/materials/memory/MemoryViews'
import {
  MEMORY_PAGE_SIZE,
  memoryPagingSummary,
  mergeMemoryPages,
  nextMemoryOffset,
} from '@/features/materials/memory/memory-pagination'
import {
  ApiError,
  api,
  collectionItems,
  type ExecutionSummary,
  type MaterialDetail,
  type MaterialSubmission,
  type MaterialSummary,
  type MaterialType,
} from '@/features/shared/api'
import { ExecutionFailure } from '@/features/shared/ExecutionFailure'
import { formatBytes, formatFrameRate, formatNumber } from '@/i18n/formatters'

const videoTabs = ['timeline', 'story', 'dialogue', 'technical'] as const
const musicTabs = ['structure', 'technical'] as const
const blockedDataKeys = /(fingerprint|hash|path|prompt|checkpoint|api.?key|secret)/i
const memorySelectionParams = ['segment', 'shot'] as const
const activeExecutionStatuses = new Set([
  'queued',
  'running',
  'analysing',
  'retrying',
  'stopping',
])

function isActiveExecution(execution: ExecutionSummary | null | undefined) {
  return Boolean(
    execution &&
    activeExecutionStatuses.has(execution.attempt.status.trim().toLocaleLowerCase()),
  )
}

function isActiveMaterial(material: MaterialSummary) {
  return activeExecutionStatuses.has(material.condition.trim().toLocaleLowerCase())
}

function queryKey(type: MaterialType, search: string, sort: string) {
  return ['materials', type, search, sort] as const
}

function withoutMemorySelection(search: string) {
  const params = new URLSearchParams(search)
  memorySelectionParams.forEach((key) => params.delete(key))
  const value = params.toString()
  return value ? `?${value}` : ''
}

function withMemorySelection(search: string, segmentId: string, shotId = '') {
  const params = new URLSearchParams(withoutMemorySelection(search))
  if (segmentId) params.set('segment', segmentId)
  if (shotId) params.set('shot', shotId)
  const value = params.toString()
  return value ? `?${value}` : ''
}

function timelineHasSegment(payload: Record<string, unknown>, segmentId: string) {
  const segments = payload.segments
  if (typeof segments !== 'object' || segments === null || Array.isArray(segments)) {
    return false
  }
  const items = (segments as Record<string, unknown>).items
  return (
    Array.isArray(items) &&
    items.some(
      (item) =>
        typeof item === 'object' &&
        item !== null &&
        !Array.isArray(item) &&
        (item as Record<string, unknown>).segment_id === segmentId,
    )
  )
}

function formatDuration(value: number | null | undefined) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null
  const rounded = Math.max(0, Math.round(value))
  const hours = Math.floor(rounded / 3600)
  const minutes = Math.floor((rounded % 3600) / 60)
  const seconds = rounded % 60
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
    : `${minutes}:${String(seconds).padStart(2, '0')}`
}

function MaterialPreview({ material }: { material: MaterialSummary }) {
  const preview =
    material.material_type === 'video' ? material.thumbnail_url : material.waveform_url
  const [failedPreview, setFailedPreview] = useState<string | null>(null)
  const condition = material.condition.trim().toLocaleLowerCase()

  if (preview && failedPreview !== preview) {
    return (
      <img
        className="material-preview__image"
        src={preview}
        alt=""
        loading="lazy"
        decoding="async"
        onError={() => setFailedPreview(preview)}
      />
    )
  }

  const isActive = ['uploading', 'queued', 'analysing'].includes(condition)
  const Icon = isActive
    ? LoaderCircle
    : condition === 'failed'
      ? AlertTriangle
      : condition === 'inconsistent'
        ? OctagonX
        : material.material_type === 'video'
          ? Film
          : AudioWaveform
  return (
    <div
      className={`material-preview__fallback material-preview__fallback--${material.material_type} material-preview__fallback--${condition}`}
      data-preview-state={condition}
      aria-hidden="true"
    >
      <Icon
        className={isActive ? 'spin' : undefined}
        size={material.material_type === 'video' ? 34 : 42}
        strokeWidth={1.4}
        aria-hidden="true"
      />
    </div>
  )
}

function MaterialCard({
  material,
  search,
}: {
  material: MaterialSummary
  search: string
}) {
  const { t } = useTranslation('common')
  const duration = formatDuration(material.duration_sec)
  return (
    <Link
      className="material-card"
      to={`${appRoutes.material(material.material_type, material.material_id)}${search}`}
      state={{ overlayParent: `/materials/${material.material_type}${search}` }}
    >
      <div className="material-preview">
        <MaterialPreview material={material} />
        {duration ? (
          <span className="material-preview__duration">{duration}</span>
        ) : null}
      </div>
      <div className="material-card__body">
        <h3>{material.name}</h3>
        <div className="material-card__status">
          <StatusBadge status={material.condition} />
          {typeof material.reference_count === 'number' ? (
            <span className="material-card__references">
              {material.reference_count} {t('common.references')}
            </span>
          ) : null}
        </div>
      </div>
    </Link>
  )
}

function EmptyMaterials({
  type,
  onImport,
}: {
  type: MaterialType
  onImport: () => void
}) {
  const { t } = useTranslation('common')
  const Icon = type === 'video' ? Film : FileAudio2
  return (
    <section className="empty-panel">
      <Icon size={28} aria-hidden="true" />
      <h2>{t(type === 'video' ? 'materials.emptyVideo' : 'materials.emptyMusic')}</h2>
      <p>{t('materials.emptyImportHelp')}</p>
      <button className="button button--primary" type="button" onClick={onImport}>
        <UploadCloud size={16} aria-hidden="true" />
        {t('materials.import')}
      </button>
    </section>
  )
}

function formatDefinitionValue(label: string, value: unknown, locale: string) {
  if (label === 'size_bytes' && typeof value === 'number') {
    return formatBytes(value, locale)
  }
  if ((label === 'frame_rate' || label === 'fps') && typeof value === 'number') {
    return formatFrameRate(value, locale)
  }
  if (label === 'tempo_bpm' && typeof value === 'number') {
    return `${formatNumber(value, locale)} BPM`
  }
  if (typeof value === 'number') return formatNumber(value, locale)
  return String(value)
}

function DefinitionList({ entries }: { entries: Array<[string, unknown]> }) {
  const { t, i18n } = useTranslation('common')
  const visible = entries.filter(
    ([, value]) => value !== null && value !== undefined && value !== '',
  )
  if (visible.length === 0) return null
  return (
    <dl className="definition-list">
      {visible.map(([label, value]) => (
        <div key={label}>
          <dt>
            {t(`materials.fields.${label}`, {
              defaultValue: label.replaceAll('_', ' '),
            })}
          </dt>
          <dd>{formatDefinitionValue(label, value, i18n.language)}</dd>
        </div>
      ))}
    </dl>
  )
}

function blockerText(blocker: unknown) {
  if (typeof blocker === 'string') return blocker
  if (typeof blocker !== 'object' || blocker === null || Array.isArray(blocker)) {
    return null
  }
  const value = blocker as Record<string, unknown>
  for (const key of ['detail', 'message', 'reason', 'label']) {
    if (typeof value[key] === 'string' && value[key]) return value[key]
  }
  const identity = [
    value.type,
    value.project_name,
    value.project_id,
    value.run_label,
    value.attempt_id,
    value.reference,
    value.owner_type,
    value.owner_id,
    value.status,
  ].filter((item): item is string => typeof item === 'string' && Boolean(item))
  return identity.length ? identity.join(' · ') : null
}

function MaterialExecution({ execution }: { execution: ExecutionSummary }) {
  const { t } = useTranslation('common')
  const progress = execution.job.progress
  const progressRecord =
    typeof progress === 'object' && progress !== null && !Array.isArray(progress)
      ? progress
      : null
  const completed = progressRecord?.completed
  const total = progressRecord?.total
  const hasMeasuredProgress =
    typeof completed === 'number' &&
    typeof total === 'number' &&
    Number.isFinite(completed) &&
    Number.isFinite(total) &&
    total > 0
  const phase = typeof progressRecord?.phase === 'string' ? progressRecord.phase : null
  const state = typeof progressRecord?.state === 'string' ? progressRecord.state : null
  return (
    <div className="material-execution" aria-live="polite">
      <div className="material-execution__heading">
        <span>
          {t('activity.attempt')} #{execution.attempt.sequence}
        </span>
        <StatusBadge status={execution.attempt.status} />
      </div>
      {phase || state ? (
        <p>
          {state
            ? t(`materials.analysisStates.${state}`, { defaultValue: state })
            : phase}
        </p>
      ) : null}
      {hasMeasuredProgress ? (
        <div className="material-execution__progress">
          <div
            role="progressbar"
            aria-valuemin={0}
            aria-valuemax={total}
            aria-valuenow={completed}
          >
            <span
              style={{
                width: `${Math.min(100, Math.max(0, (completed / total) * 100))}%`,
              }}
            />
          </div>
          <small>
            {completed} / {total}
          </small>
        </div>
      ) : null}
      {execution.attempt.error_message ? (
        <ExecutionFailure
          className="field-error"
          operationType={execution.attempt.operation_type}
          ownerType={execution.attempt.owner_type}
          status={execution.attempt.status}
        />
      ) : null}
    </div>
  )
}

function MaterialDrawer({
  type,
  materialId,
  escapeEnabled,
}: {
  type: MaterialType
  materialId: string
  escapeEnabled: boolean
}) {
  const { t } = useTranslation('common')
  const navigate = useNavigate()
  const location = useLocation()
  const queryClient = useQueryClient()
  const { isConnected } = useEventStream()
  const drawerRef = useRef<HTMLElement>(null)
  const [deleteArmed, setDeleteArmed] = useState(false)
  const detail = useQuery({
    queryKey: ['material', materialId],
    queryFn: () => api.materials.detail(materialId),
    refetchInterval: (query) =>
      !isConnected && isActiveExecution(query.state.data?.latest_execution)
        ? 2000
        : false,
  })
  const parent = `/materials/${type}${location.search}`
  const close = useCallback(() => {
    const state = location.state as { overlayParent?: string } | null
    if (state?.overlayParent === parent) navigate(-1)
    else navigate(parent, { replace: true })
  }, [location.state, navigate, parent])
  const refreshMaterial = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ['materials'] }),
      queryClient.invalidateQueries({ queryKey: ['material', materialId] }),
      queryClient.invalidateQueries({ queryKey: ['activity'] }),
    ])
  }
  const recovery = useMutation({
    mutationFn: (action: 'retry' | 'resume') => api.materials[action](materialId),
    onSuccess: refreshMaterial,
  })
  const stopAnalysis = useMutation({
    mutationFn: (attemptId: string) => api.attempts.stop(attemptId),
    onSuccess: refreshMaterial,
  })
  const remove = useMutation({
    mutationFn: () => api.materials.delete(materialId),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['materials'] }),
        queryClient.invalidateQueries({ queryKey: ['activity'] }),
      ])
      queryClient.removeQueries({ queryKey: ['material', materialId] })
      close()
    },
  })

  useEffect(() => {
    if (!deleteArmed) return
    const timeout = window.setTimeout(() => setDeleteArmed(false), 6000)
    return () => window.clearTimeout(timeout)
  }, [deleteArmed])

  useEffect(() => {
    if (!escapeEnabled) return
    const previouslyFocused = document.activeElement
    drawerRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close()
      if (event.key !== 'Tab' || !drawerRef.current) return
      const focusable = Array.from(
        drawerRef.current.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      )
      if (!focusable.length) {
        event.preventDefault()
        drawerRef.current.focus()
      } else if (event.shiftKey && document.activeElement === focusable[0]) {
        event.preventDefault()
        focusable.at(-1)?.focus()
      } else if (!event.shiftKey && document.activeElement === focusable.at(-1)) {
        event.preventDefault()
        focusable[0]?.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      if (previouslyFocused instanceof HTMLElement) previouslyFocused.focus()
    }
  }, [close, escapeEnabled])
  return (
    <div
      className="drawer-layer"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) close()
      }}
    >
      <aside
        ref={drawerRef}
        className="material-drawer"
        aria-label={t('materials.drawerSummary')}
        tabIndex={-1}
      >
        <header className="drawer-header">
          <div>
            <span className="eyebrow">
              {type === 'video' ? t('nav.video') : t('nav.music')}
            </span>
            <h2>{detail.data?.name ?? t('materials.drawerSummary')}</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            onClick={close}
            aria-label={t('common.close')}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>
        {detail.isPending ? <LoadingState /> : null}
        {detail.isError ? <ErrorState onRetry={() => void detail.refetch()} /> : null}
        {detail.data ? (
          <MaterialDrawerContent
            detail={detail.data}
            search={location.search}
            deleteArmed={deleteArmed}
            operationPending={
              recovery.isPending || stopAnalysis.isPending || remove.isPending
            }
            operationError={remove.error ?? stopAnalysis.error ?? recovery.error}
            onRecover={(action) => {
              remove.reset()
              stopAnalysis.reset()
              recovery.mutate(action)
            }}
            onStop={(attemptId) => {
              remove.reset()
              recovery.reset()
              stopAnalysis.mutate(attemptId)
            }}
            onDelete={() => {
              if (deleteArmed) {
                recovery.reset()
                stopAnalysis.reset()
                remove.mutate()
              } else setDeleteArmed(true)
            }}
          />
        ) : null}
      </aside>
    </div>
  )
}

function MaterialDrawerContent({
  detail,
  search,
  deleteArmed,
  operationPending,
  operationError,
  onRecover,
  onStop,
  onDelete,
}: {
  detail: MaterialDetail
  search: string
  deleteArmed: boolean
  operationPending: boolean
  operationError: unknown
  onRecover: (action: 'retry' | 'resume') => void
  onStop: (attemptId: string) => void
  onDelete: () => void
}) {
  const { t } = useTranslation('common')
  const defaultTab = detail.material_type === 'video' ? 'timeline' : 'structure'
  const execution = detail.latest_execution
  const executionStatus = execution?.attempt.status.trim().toLocaleLowerCase()
  const active = isActiveExecution(execution)
  const referenced = Boolean(detail.references?.length)
  const knownDeleteBlocker = referenced || active
  const serverBlockers =
    operationError instanceof ApiError &&
    Array.isArray(operationError.problem?.blockers)
      ? operationError.problem.blockers
          .map(blockerText)
          .filter((value): value is string => value !== null)
      : []
  return (
    <div className="drawer-content">
      <div className="drawer-preview material-preview">
        {detail.material_type === 'video' ? (
          <video
            controls
            muted
            preload="metadata"
            src={`/api/materials/${encodeURIComponent(detail.material_id)}/source`}
          />
        ) : (
          <div className="drawer-audio">
            <AudioWaveform size={34} aria-hidden="true" />
            {/* Music materials contain no spoken dialogue, so captions are not applicable. */}
            {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
            <audio
              controls
              preload="metadata"
              src={`/api/materials/${encodeURIComponent(detail.material_id)}/source`}
            />
          </div>
        )}
      </div>
      <div className="drawer-identity">
        <StatusBadge status={detail.condition} />
        {detail.duration_sec ? (
          <span>
            <Clock3 size={14} />
            {formatDuration(detail.duration_sec)}
          </span>
        ) : null}
      </div>
      <section className="drawer-section">
        <h3>
          <Info size={15} />
          {t('materials.sourceMetadata')}
        </h3>
        <DefinitionList entries={Object.entries(detail.source ?? {})} />
        {!detail.source ? (
          <p className="muted-copy">{t('common.notAvailable')}</p>
        ) : null}
      </section>
      <section className="drawer-section drawer-section--memory">
        <h3>
          <Sparkles size={15} />
          {t('materials.memorySummary')}
        </h3>
        {detail.memory_summary ? (
          <DefinitionList
            entries={Object.entries(detail.memory_summary).filter(
              ([key]) => !blockedDataKeys.test(key),
            )}
          />
        ) : (
          <p className="muted-copy">{t('materials.memoryUnavailable')}</p>
        )}
        {detail.analysis_available ? (
          <Link
            className="button button--primary button--wide"
            to={`${appRoutes.materialMemory(detail.material_type, detail.material_id, defaultTab)}${search}`}
            state={{
              overlayParent: `${appRoutes.material(
                detail.material_type,
                detail.material_id,
              )}${search}`,
            }}
          >
            {t('materials.fullAnalysis')}
            <ChevronRight size={16} aria-hidden="true" />
          </Link>
        ) : null}
      </section>
      <section className="drawer-section">
        <h3>{t('common.references')}</h3>
        {detail.references?.length ? (
          <ul className="reference-list">
            {detail.references.map((reference, index) => {
              const label =
                reference.kind === 'project_current'
                  ? t('materials.projectCurrentReference', {
                      project: reference.project_name,
                    })
                  : reference.kind === 'run_snapshot'
                    ? t('materials.runSnapshotReference', {
                        project: reference.project_name,
                        sequence: reference.run_sequence,
                      })
                    : t('materials.unknownReference')
              const destination =
                reference.kind === 'project_current'
                  ? appRoutes.projectOverview(reference.navigation.project_id)
                  : reference.kind === 'run_snapshot'
                    ? appRoutes.runDetail(
                        reference.navigation.project_id,
                        reference.navigation.run_id,
                      )
                    : null
              return (
                <li key={`${label}-${index}`}>
                  {destination ? (
                    <Link to={destination}>{label}</Link>
                  ) : (
                    <span>{label}</span>
                  )}
                </li>
              )
            })}
          </ul>
        ) : (
          <p className="muted-copy">{t('common.none')}</p>
        )}
      </section>
      <section className="drawer-section">
        <h3>{t('materials.analysisAttempts')}</h3>
        {execution ? <MaterialExecution execution={execution} /> : null}
        {detail.attempts?.length ? (
          detail.attempts.map((attempt) => (
            <div className="attempt-row" key={attempt.attempt_id}>
              <span>
                {t('activity.attempt')} {attempt.sequence}
              </span>
              <StatusBadge status={attempt.status} />
            </div>
          ))
        ) : (
          <p className="muted-copy">{t('common.none')}</p>
        )}
      </section>
      <section className="drawer-section material-actions">
        <h3>{t('materials.actions')}</h3>
        <div className="material-actions__buttons">
          {executionStatus === 'failed' ? (
            <button
              className="button button--secondary"
              type="button"
              disabled={operationPending}
              onClick={() => onRecover('retry')}
            >
              {operationPending ? (
                <LoaderCircle className="spin" size={15} aria-hidden="true" />
              ) : (
                <RefreshCcw size={15} aria-hidden="true" />
              )}
              {t('common.retry')}
            </button>
          ) : null}
          {executionStatus === 'interrupted' ? (
            <button
              className="button button--secondary"
              type="button"
              disabled={operationPending}
              onClick={() => onRecover('resume')}
            >
              {operationPending ? (
                <LoaderCircle className="spin" size={15} aria-hidden="true" />
              ) : (
                <RefreshCcw size={15} aria-hidden="true" />
              )}
              {t('materials.resumeAnalysis')}
            </button>
          ) : null}
          {active && execution ? (
            <button
              className="button button--secondary"
              type="button"
              disabled={operationPending || executionStatus === 'stopping'}
              onClick={() => onStop(execution.attempt.attempt_id)}
            >
              {operationPending ? (
                <LoaderCircle className="spin" size={15} aria-hidden="true" />
              ) : (
                <OctagonX size={15} aria-hidden="true" />
              )}
              {executionStatus === 'stopping'
                ? t('activity.stopping')
                : t('activity.stopAttempt')}
            </button>
          ) : null}
          <button
            className="button material-delete-button"
            type="button"
            disabled={operationPending || knownDeleteBlocker}
            onClick={onDelete}
          >
            {operationPending ? (
              <LoaderCircle className="spin" size={15} aria-hidden="true" />
            ) : (
              <Trash2 size={15} aria-hidden="true" />
            )}
            {deleteArmed ? t('materials.confirmDelete') : t('materials.delete')}
          </button>
        </div>
        {active ? (
          <p className="truthful-note">{t('materials.deleteBlockedActive')}</p>
        ) : null}
        {referenced ? (
          <p className="truthful-note">{t('materials.deleteBlockedReferences')}</p>
        ) : null}
        {deleteArmed && !knownDeleteBlocker ? (
          <p className="material-delete-confirm" role="status">
            {t('materials.clickDeleteAgain')}
          </p>
        ) : null}
        {serverBlockers.length ? (
          <ul className="material-delete-blockers">
            {serverBlockers.map((blocker, index) => (
              <li key={`${blocker}-${index}`}>{blocker}</li>
            ))}
          </ul>
        ) : null}
        {operationError && !serverBlockers.length ? (
          <p className="field-error" role="alert">
            {operationError instanceof Error
              ? operationError.message
              : t('materials.operationFailed')}
          </p>
        ) : null}
      </section>
    </div>
  )
}

function MemoryExplorer({
  type,
  materialId,
  tab,
}: {
  type: MaterialType
  materialId: string
  tab: string
}) {
  const { t } = useTranslation('common')
  const navigate = useNavigate()
  const location = useLocation()
  const modalRef = useRef<HTMLElement>(null)
  const tabs = type === 'video' ? videoTabs : musicTabs
  const activeTab = tabs.includes(tab as never) ? tab : tabs[0]
  const memory = useInfiniteQuery({
    queryKey: ['material-memory', materialId, activeTab, MEMORY_PAGE_SIZE],
    initialPageParam: 0,
    queryFn: ({ pageParam }) =>
      api.materials.memory(materialId, activeTab, {
        limit: MEMORY_PAGE_SIZE,
        offset: pageParam,
      }),
    getNextPageParam: (lastPage) => nextMemoryOffset(type, activeTab, lastPage),
    retry: false,
  })
  const mergedMemory = useMemo(
    () => mergeMemoryPages(type, activeTab, memory.data?.pages ?? []),
    [activeTab, memory.data?.pages, type],
  )
  const paging = mergedMemory
    ? memoryPagingSummary(type, activeTab, mergedMemory)
    : null
  const baseSearch = withoutMemorySelection(location.search)
  const selection = new URLSearchParams(location.search)
  const selectedSegmentId = selection.get('segment') ?? ''
  const selectedShotId = selection.get('shot') ?? ''
  const parent = `${appRoutes.material(type, materialId)}${baseSearch}`
  const close = useCallback(() => {
    const state = location.state as { overlayParent?: string } | null
    if (state?.overlayParent === parent) navigate(-1)
    else navigate(parent, { replace: true })
  }, [location.state, navigate, parent])

  const updateTimelineSelection = useCallback(
    (segmentId: string, shotId = '') => {
      navigate(
        `${appRoutes.materialMemory(type, materialId, 'timeline')}${withMemorySelection(
          location.search,
          segmentId,
          shotId,
        )}`,
        { replace: true, state: location.state },
      )
    },
    [location.search, location.state, materialId, navigate, type],
  )

  const openTimelineSegment = useCallback(
    (segmentId: string) => {
      navigate(
        `${appRoutes.materialMemory(type, materialId, 'timeline')}${withMemorySelection(
          location.search,
          segmentId,
        )}`,
        { state: location.state },
      )
    },
    [location.search, location.state, materialId, navigate, type],
  )

  useEffect(() => {
    if (tab === activeTab) return
    navigate(`${appRoutes.materialMemory(type, materialId, activeTab)}${baseSearch}`, {
      replace: true,
      state: location.state,
    })
  }, [activeTab, baseSearch, location.state, materialId, navigate, tab, type])

  useEffect(() => {
    if (
      activeTab !== 'timeline' ||
      !selectedSegmentId ||
      !mergedMemory ||
      timelineHasSegment(mergedMemory.payload, selectedSegmentId) ||
      !memory.hasNextPage ||
      memory.isFetchingNextPage
    ) {
      return
    }
    void memory.fetchNextPage()
  }, [activeTab, memory, mergedMemory, selectedSegmentId])

  useEffect(() => {
    const previouslyFocused = document.activeElement
    modalRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close()
      if (event.key !== 'Tab' || !modalRef.current) return
      const focusable = Array.from(
        modalRef.current.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      )
      if (!focusable.length) {
        event.preventDefault()
        modalRef.current.focus()
      } else if (event.shiftKey && document.activeElement === focusable[0]) {
        event.preventDefault()
        focusable.at(-1)?.focus()
      } else if (!event.shiftKey && document.activeElement === focusable.at(-1)) {
        event.preventDefault()
        focusable[0]?.focus()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      if (previouslyFocused instanceof HTMLElement) previouslyFocused.focus()
    }
  }, [close])
  return (
    <div
      className="memory-modal-layer"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) close()
      }}
    >
      <section
        ref={modalRef}
        className="memory-modal"
        role="dialog"
        aria-modal="true"
        aria-label={t('materials.memoryExplorer')}
        tabIndex={-1}
      >
        <header className="memory-modal__header">
          <div>
            <span className="eyebrow">MASTER · M</span>
            <h2>{t('materials.memoryExplorer')}</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            onClick={close}
            aria-label={t('common.close')}
          >
            <X size={19} aria-hidden="true" />
          </button>
        </header>
        <nav className="memory-tabs" aria-label={t('materials.memoryExplorer')}>
          {tabs.map((item) => (
            <Link
              key={item}
              className={
                item === activeTab ? 'memory-tab memory-tab--active' : 'memory-tab'
              }
              to={`${appRoutes.materialMemory(type, materialId, item)}${baseSearch}`}
              state={location.state}
              replace
            >
              {t(`materials.${item}`)}
            </Link>
          ))}
        </nav>
        <div className={`memory-modal__body memory-modal__body--${activeTab}`}>
          {memory.isPending ? <LoadingState /> : null}
          {memory.isError ? (
            memory.error instanceof ApiError &&
            ([404, 501].includes(memory.error.status) ||
              memory.error.problem?.code === 'material_memory_unavailable') ? (
              <div className="unavailable-panel">
                <AlertTriangle size={24} aria-hidden="true" />
                <h3>{t('common.unavailable')}</h3>
                <p>{t('materials.memoryUnavailable')}</p>
              </div>
            ) : (
              <ErrorState onRetry={() => void memory.refetch()} />
            )
          ) : null}
          {mergedMemory ? (
            <MemoryTabView
              type={type}
              tab={activeTab}
              materialId={materialId}
              payload={mergedMemory.payload}
              selectedSegmentId={selectedSegmentId}
              selectedShotId={selectedShotId}
              onTimelineSelectionChange={updateTimelineSelection}
              onOpenTimelineSegment={openTimelineSegment}
            />
          ) : null}
          {paging ? (
            <footer className="memory-pagination" aria-live="polite">
              <span>
                {t('materials.loadedCount', {
                  loaded: paging.loaded,
                  total: paging.total,
                })}
              </span>
              {memory.hasNextPage ? (
                <button
                  className="button button--secondary"
                  type="button"
                  disabled={memory.isFetchingNextPage}
                  onClick={() => void memory.fetchNextPage()}
                >
                  {memory.isFetchingNextPage ? (
                    <LoaderCircle className="spin" size={15} aria-hidden="true" />
                  ) : null}
                  {memory.isFetchingNextPage
                    ? t('common.loading')
                    : t('materials.loadMore')}
                </button>
              ) : null}
            </footer>
          ) : null}
        </div>
      </section>
    </div>
  )
}

export function MaterialsWorkspace() {
  const { t } = useTranslation('common')
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { isConnected } = useEventStream()
  const params = useParams<{ type?: string; materialId?: string; tab?: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const [importOpen, setImportOpen] = useState(false)
  const type: MaterialType = params.type === 'music' ? 'music' : 'video'
  const search = searchParams.get('search') ?? ''
  const sort = searchParams.get('sort') ?? 'name_asc'
  const list = useQuery({
    queryKey: queryKey(type, search, sort),
    queryFn: () => api.materials.list(type, search, sort),
    refetchInterval: (query) => {
      if (isConnected) return false
      const value = query.state.data
      return value && collectionItems(value).some(isActiveMaterial) ? 2000 : false
    },
  })
  const items = useMemo(() => {
    const values = list.data ? collectionItems(list.data) : []
    const needle = search.trim().toLocaleLowerCase()
    const filtered = needle
      ? values.filter((item) => item.name.toLocaleLowerCase().includes(needle))
      : values
    return [...filtered].sort((left, right) => {
      const comparison = left.name.localeCompare(right.name)
      return sort === 'name_desc' ? -comparison : comparison
    })
  }, [list.data, search, sort])
  const location = useLocation()

  const closeImport = useCallback(() => setImportOpen(false), [])
  const handleCreated = useCallback(
    async (submission: MaterialSubmission) => {
      const material = submission.material
      setImportOpen(false)
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['materials'] }),
        queryClient.invalidateQueries({ queryKey: ['activity'] }),
      ])
      const listParent = `/materials/${material.material_type}${location.search}`
      navigate(
        `${appRoutes.material(material.material_type, material.material_id)}${location.search}`,
        { state: { overlayParent: listParent } },
      )
    },
    [location.search, navigate, queryClient],
  )
  const handleViewExisting = useCallback(
    (material: MaterialSummary) => {
      setImportOpen(false)
      const listParent = `/materials/${material.material_type}${location.search}`
      navigate(
        `${appRoutes.material(material.material_type, material.material_id)}${location.search}`,
        { state: { overlayParent: listParent } },
      )
    },
    [location.search, navigate],
  )

  const setFilter = (key: string, value: string) => {
    const next = new URLSearchParams(searchParams)
    if (value) next.set(key, value)
    else next.delete(key)
    setSearchParams(next, { replace: true })
  }

  return (
    <div className="page page--materials">
      <header className="page-header">
        <div>
          <span className="eyebrow">M · Material Analyst</span>
          <h1>{t('materials.title')}</h1>
          <p>{t('materials.subtitle')}</p>
        </div>
        <button
          className="button button--primary"
          type="button"
          onClick={() => setImportOpen(true)}
        >
          <UploadCloud size={16} aria-hidden="true" />
          {t('materials.import')}
        </button>
      </header>
      <div className="material-type-tabs">
        <Link
          className={type === 'video' ? 'type-tab type-tab--active' : 'type-tab'}
          to={`/materials/video${location.search}`}
        >
          <Film size={16} aria-hidden="true" />
          {t('nav.video')}
        </Link>
        <Link
          className={type === 'music' ? 'type-tab type-tab--active' : 'type-tab'}
          to={`/materials/music${location.search}`}
        >
          <AudioWaveform size={17} aria-hidden="true" />
          {t('nav.music')}
        </Link>
      </div>
      <div className="toolbar">
        <label className="search-control">
          <Search size={16} aria-hidden="true" />
          <input
            value={search}
            onChange={(event) => setFilter('search', event.target.value)}
            placeholder={t('materials.searchPlaceholder')}
            aria-label={t('common.search')}
          />
        </label>
        <label className="sort-control">
          <SlidersHorizontal size={15} aria-hidden="true" />
          <select
            value={sort}
            onChange={(event) => setFilter('sort', event.target.value)}
            aria-label={t('common.status')}
          >
            <option value="name_asc">{t('common.name')}</option>
            <option value="name_desc">{t('common.name')} ↓</option>
          </select>
        </label>
      </div>
      {list.isPending ? <LoadingState /> : null}
      {list.isError ? <ErrorState onRetry={() => void list.refetch()} /> : null}
      {list.isSuccess && items.length === 0 ? (
        <EmptyMaterials type={type} onImport={() => setImportOpen(true)} />
      ) : null}
      {items.length > 0 ? (
        <section className="material-grid" aria-live="polite">
          {items.map((material) => (
            <MaterialCard
              key={material.material_id}
              material={material}
              search={location.search}
            />
          ))}
        </section>
      ) : null}
      {params.materialId ? (
        <MaterialDrawer
          type={type}
          materialId={params.materialId}
          escapeEnabled={!params.tab}
        />
      ) : null}
      {params.materialId && params.tab ? (
        <MemoryExplorer type={type} materialId={params.materialId} tab={params.tab} />
      ) : null}
      {importOpen ? (
        <MaterialImportDialog
          initialType={type}
          onClose={closeImport}
          onCreated={(submission) => void handleCreated(submission)}
          onViewExisting={handleViewExisting}
        />
      ) : null}
    </div>
  )
}
