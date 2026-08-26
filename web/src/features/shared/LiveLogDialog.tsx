import { useMutation, useQuery } from '@tanstack/react-query'
import { FileText, Radio, Rows3, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  api,
  type AttemptLogEntry,
  type AttemptLogPage,
  type ExecutionSummary,
} from '@/features/shared/api'

const LOG_LEVELS: AttemptLogEntry['level'][] = [
  'DEBUG',
  'INFO',
  'SUCCESS',
  'WARNING',
  'ERROR',
  'CRITICAL',
  'RAW',
]
const ALL_FILTER = '__all__'
const UNKNOWN_TASK = '__unknown__'

export function ExecutionLogAccess({
  execution,
  className = '',
}: {
  execution: ExecutionSummary | null | undefined
  className?: string
}) {
  const { t } = useTranslation('common')
  const [open, setOpen] = useState(false)
  if (!execution?.log) return null
  return (
    <section className={`execution-log-access ${className}`.trim()}>
      <div className="execution-log-access__path">
        <span>{t('executionLogs.path')}</span>
        <code title={execution.log.path}>{execution.log.path}</code>
      </div>
      <button
        className="button button--secondary"
        type="button"
        onClick={() => setOpen(true)}
      >
        <FileText size={15} aria-hidden="true" />
        {t('executionLogs.open')}
      </button>
      {open ? (
        <LiveLogDialog
          attemptId={execution.attempt.attempt_id}
          initialPath={execution.log.path}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </section>
  )
}

function LiveLogDialog({
  attemptId,
  initialPath,
  onClose,
}: {
  attemptId: string
  initialPath: string
  onClose: () => void
}) {
  const { t } = useTranslation('common')
  const [liveEntries, setLiveEntries] = useState<AttemptLogEntry[]>([])
  const [fullPage, setFullPage] = useState<AttemptLogPage | null>(null)
  const [reset, setReset] = useState(false)
  const [ended, setEnded] = useState(false)
  const [levelFilter, setLevelFilter] = useState(ALL_FILTER)
  const [taskFilter, setTaskFilter] = useState(ALL_FILTER)
  const viewportRef = useRef<HTMLDivElement>(null)
  const query = useQuery({
    queryKey: ['attempt-log', attemptId],
    queryFn: () => api.attempts.logs(attemptId),
    staleTime: 0,
    refetchOnMount: 'always',
  })
  const fullLog = useMutation({
    mutationFn: () => api.attempts.fullLogs(attemptId),
    onSuccess: (page) => {
      setFullPage(page)
      setLiveEntries([])
      setReset(false)
    },
  })
  const snapshot = fullPage ?? query.data

  useEffect(() => {
    const page = snapshot
    if (!page) return
    if (typeof EventSource === 'undefined') return
    const source = new EventSource(
      api.attempts.logStreamUrl(attemptId, page.end_cursor),
    )
    const onEntry = (event: MessageEvent<string>) => {
      try {
        const entry = JSON.parse(event.data) as AttemptLogEntry
        setLiveEntries((current) => [
          ...(fullPage === null ? current.slice(-1999) : current),
          entry,
        ])
      } catch {
        // Ignore malformed transport frames; the persisted log remains authoritative.
      }
    }
    const onReset = () => {
      setReset(true)
      setLiveEntries([])
    }
    const onEnd = () => {
      setEnded(true)
      source.close()
    }
    source.addEventListener('log_entry', onEntry as EventListener)
    source.addEventListener('log_reset', onReset)
    source.addEventListener('log_end', onEnd)
    return () => source.close()
  }, [attemptId, fullPage, snapshot])

  const entries = useMemo(
    () =>
      mergeEntries(
        reset ? liveEntries : [...(snapshot?.entries ?? []), ...liveEntries],
      ),
    [liveEntries, reset, snapshot?.entries],
  )
  const taskOptions = useMemo(
    () =>
      Array.from(new Set(entries.map((entry) => entry.component ?? UNKNOWN_TASK))).sort(
        (left, right) => left.localeCompare(right),
      ),
    [entries],
  )
  const filteredEntries = useMemo(
    () =>
      entries.filter(
        (entry) =>
          (levelFilter === ALL_FILTER || entry.level === levelFilter) &&
          (taskFilter === ALL_FILTER ||
            (entry.component ?? UNKNOWN_TASK) === taskFilter),
      ),
    [entries, levelFilter, taskFilter],
  )

  useEffect(() => {
    const viewport = viewportRef.current
    if (viewport) viewport.scrollTop = viewport.scrollHeight
  }, [entries])

  useEffect(() => {
    const listener = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', listener)
    return () => window.removeEventListener('keydown', listener)
  }, [onClose])

  const path = snapshot?.log.path ?? initialPath
  const hasOutput = Boolean(snapshot?.log.exists || entries.length > 0)
  return (
    <div
      className="dialog-layer execution-log-layer"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose()
      }}
    >
      <section
        className="execution-log-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="execution-log-title"
      >
        <header className="execution-log-dialog__header">
          <div className="execution-log-dialog__identity">
            <span className="eyebrow">{t('executionLogs.eyebrow')}</span>
            <h2 id="execution-log-title">{t('executionLogs.title')}</h2>
            <code title={path}>{path}</code>
          </div>
          <div className="execution-log-dialog__header-actions">
            <button
              className="button button--secondary"
              type="button"
              disabled={fullLog.isPending || fullPage !== null}
              onClick={() => fullLog.mutate()}
            >
              <Rows3 size={15} aria-hidden="true" />
              {fullPage ? t('executionLogs.fullLoaded') : t('executionLogs.readFull')}
            </button>
            <button
              className="icon-button"
              type="button"
              aria-label={t('common.close')}
              onClick={onClose}
            >
              <X aria-hidden="true" />
            </button>
          </div>
        </header>
        <div className="execution-log-dialog__status" aria-live="polite">
          <Radio size={14} aria-hidden="true" />
          {ended
            ? t('executionLogs.ended')
            : hasOutput
              ? t('executionLogs.live')
              : t('executionLogs.waiting')}
        </div>
        <div className="execution-log-dialog__filters">
          <label>
            <span>{t('executionLogs.levelFilter')}</span>
            <select
              value={levelFilter}
              onChange={(event) => setLevelFilter(event.target.value)}
            >
              <option value={ALL_FILTER}>{t('executionLogs.allLevels')}</option>
              {LOG_LEVELS.map((level) => (
                <option key={level} value={level}>
                  {level}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>{t('executionLogs.taskFilter')}</span>
            <select
              value={taskFilter}
              onChange={(event) => setTaskFilter(event.target.value)}
            >
              <option value={ALL_FILTER}>{t('executionLogs.allTasks')}</option>
              {taskOptions.map((task) => (
                <option key={task} value={task}>
                  {task === UNKNOWN_TASK ? t('executionLogs.unknownTask') : task}
                </option>
              ))}
            </select>
          </label>
          {fullLog.isError ? (
            <span className="execution-log-dialog__filter-error" role="alert">
              {t('executionLogs.fullLoadFailed')}
            </span>
          ) : null}
        </div>
        <div className="execution-log-dialog__viewport" ref={viewportRef}>
          {query.isPending ? <p>{t('common.loading')}</p> : null}
          {query.isError ? <p role="alert">{t('executionLogs.loadFailed')}</p> : null}
          {!query.isPending && !query.isError && entries.length === 0 ? (
            <p className="execution-log-dialog__empty">{t('executionLogs.empty')}</p>
          ) : null}
          {entries.length > 0 && filteredEntries.length === 0 ? (
            <p className="execution-log-dialog__empty">
              {t('executionLogs.noMatches')}
            </p>
          ) : null}
          {filteredEntries.map((entry) => (
            <div
              className={`execution-log-line execution-log-line--${entry.level.toLowerCase()}`}
              key={entry.cursor}
            >
              <span className="execution-log-line__timestamp">
                {entry.timestamp ?? '—'}
              </span>
              <strong>{entry.level}</strong>
              <span className="execution-log-line__component">
                {[entry.component, entry.event].filter(Boolean).join(' · ') ||
                  t('executionLogs.unknownTask')}
              </span>
              <span className="execution-log-line__message">
                {entry.fields ? `${entry.fields} · ` : ''}
                {entry.message}
              </span>
            </div>
          ))}
        </div>
      </section>
    </div>
  )
}

function mergeEntries(entries: AttemptLogEntry[]): AttemptLogEntry[] {
  const byCursor = new Map<number, AttemptLogEntry>()
  for (const entry of entries) byCursor.set(entry.cursor, entry)
  return Array.from(byCursor.values()).sort((left, right) => left.cursor - right.cursor)
}
