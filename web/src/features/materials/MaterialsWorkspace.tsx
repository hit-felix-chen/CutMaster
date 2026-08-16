import { useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  AudioWaveform,
  ChevronRight,
  Clock3,
  FileAudio2,
  Film,
  Info,
  Search,
  SlidersHorizontal,
  Sparkles,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Link,
  useLocation,
  useNavigate,
  useParams,
  useSearchParams,
} from 'react-router-dom'

import { appRoutes } from '@/app/routes'
import { ErrorState, LoadingState } from '@/components/ui/AsyncState'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { MemoryTabView } from '@/features/materials/memory/MemoryViews'
import {
  ApiError,
  api,
  collectionItems,
  type MaterialDetail,
  type MaterialSummary,
  type MaterialType,
} from '@/features/shared/api'
import { formatBytes, formatFrameRate, formatNumber } from '@/i18n/formatters'

const videoTabs = ['timeline', 'story', 'dialogue', 'technical'] as const
const musicTabs = ['structure', 'technical'] as const
const blockedDataKeys = /(fingerprint|hash|path|prompt|checkpoint|api.?key|secret)/i

function queryKey(type: MaterialType, search: string, sort: string) {
  return ['materials', type, search, sort] as const
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
  if (preview) {
    return <img className="material-preview__image" src={preview} alt="" />
  }
  return (
    <div
      className={`material-preview__fallback material-preview__fallback--${material.material_type}`}
    >
      {material.material_type === 'video' ? (
        <Film size={34} strokeWidth={1.4} aria-hidden="true" />
      ) : (
        <AudioWaveform size={42} strokeWidth={1.3} aria-hidden="true" />
      )}
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

function EmptyMaterials({ type }: { type: MaterialType }) {
  const { t } = useTranslation('common')
  const Icon = type === 'video' ? Film : FileAudio2
  return (
    <section className="empty-panel">
      <Icon size={28} aria-hidden="true" />
      <h2>{t(type === 'video' ? 'materials.emptyVideo' : 'materials.emptyMusic')}</h2>
      <p>{t('materials.importUnavailable')}</p>
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
  const drawerRef = useRef<HTMLElement>(null)
  const detail = useQuery({
    queryKey: ['material', materialId],
    queryFn: () => api.materials.detail(materialId),
  })
  const parent = `/materials/${type}${location.search}`
  const close = useCallback(() => {
    const state = location.state as { overlayParent?: string } | null
    if (state?.overlayParent === parent) navigate(-1)
    else navigate(parent, { replace: true })
  }, [location.state, navigate, parent])

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
          <MaterialDrawerContent detail={detail.data} search={location.search} />
        ) : null}
      </aside>
    </div>
  )
}

function MaterialDrawerContent({
  detail,
  search,
}: {
  detail: MaterialDetail
  search: string
}) {
  const { t } = useTranslation('common')
  const defaultTab = detail.material_type === 'video' ? 'timeline' : 'structure'
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
                typeof reference === 'string'
                  ? reference
                  : (reference.project_name ??
                    reference.project_id ??
                    t('common.unknown'))
              const runLabel =
                typeof reference === 'string' ? null : reference.run_label
              return (
                <li key={`${label}-${index}`}>
                  <span>{label}</span>
                  {runLabel ? <small>{runLabel}</small> : null}
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
  const memory = useQuery({
    queryKey: ['material-memory', materialId, activeTab],
    queryFn: () => api.materials.memory(materialId, activeTab),
    retry: false,
  })
  const parent = `${appRoutes.material(type, materialId)}${location.search}`
  const close = useCallback(() => {
    const state = location.state as { overlayParent?: string } | null
    if (state?.overlayParent === parent) navigate(-1)
    else navigate(parent, { replace: true })
  }, [location.state, navigate, parent])

  useEffect(() => {
    if (tab === activeTab) return
    navigate(
      `${appRoutes.materialMemory(type, materialId, activeTab)}${location.search}`,
      { replace: true, state: location.state },
    )
  }, [activeTab, location.search, location.state, materialId, navigate, tab, type])

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
              to={`${appRoutes.materialMemory(type, materialId, item)}${location.search}`}
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
          {memory.data ? (
            <MemoryTabView
              type={type}
              tab={activeTab}
              materialId={materialId}
              payload={memory.data.payload}
            />
          ) : null}
        </div>
      </section>
    </div>
  )
}

export function MaterialsWorkspace() {
  const { t } = useTranslation('common')
  const params = useParams<{ type?: string; materialId?: string; tab?: string }>()
  const [searchParams, setSearchParams] = useSearchParams()
  const type: MaterialType = params.type === 'music' ? 'music' : 'video'
  const search = searchParams.get('search') ?? ''
  const sort = searchParams.get('sort') ?? 'name_asc'
  const list = useQuery({
    queryKey: queryKey(type, search, sort),
    queryFn: () => api.materials.list(type, search, sort),
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
      {list.isSuccess && items.length === 0 ? <EmptyMaterials type={type} /> : null}
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
    </div>
  )
}
