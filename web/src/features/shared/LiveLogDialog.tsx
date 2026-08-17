import { useQuery } from '@tanstack/react-query'
import { FileText, Radio, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { api, type AttemptLogEntry, type ExecutionSummary } from '@/features/shared/api'

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
  const [reset, setReset] = useState(false)
  const [ended, setEnded] = useState(false)
  const viewportRef = useRef<HTMLDivElement>(null)
  const query = useQuery({
    queryKey: ['attempt-log', attemptId],
    queryFn: () => api.attempts.logs(attemptId),
    staleTime: 0,
    refetchOnMount: 'always',
  })

  useEffect(() => {
    const page = query.data
    if (!page) return
    if (typeof EventSource === 'undefined') return
    const source = new EventSource(
      api.attempts.logStreamUrl(attemptId, page.end_cursor),
    )
    const onEntry = (event: MessageEvent<string>) => {
      try {
        const entry = JSON.parse(event.data) as AttemptLogEntry
        setLiveEntries((current) => [...current.slice(-1999), entry])
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
  }, [attemptId, query.data])

  const entries = useMemo(
    () => (reset ? liveEntries : [...(query.data?.entries ?? []), ...liveEntries]),
    [liveEntries, query.data?.entries, reset],
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

  const path = query.data?.log.path ?? initialPath
  const hasOutput = Boolean(query.data?.log.exists || entries.length > 0)
  return (
    <div className="dialog-layer execution-log-layer">
      <section
        className="execution-log-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="execution-log-title"
      >
        <header className="execution-log-dialog__header">
          <div>
            <span className="eyebrow">{t('executionLogs.eyebrow')}</span>
            <h2 id="execution-log-title">{t('executionLogs.title')}</h2>
            <code title={path}>{path}</code>
          </div>
          <button
            className="icon-button"
            type="button"
            aria-label={t('common.close')}
            onClick={onClose}
          >
            <X aria-hidden="true" />
          </button>
        </header>
        <div className="execution-log-dialog__status" aria-live="polite">
          <Radio size={14} aria-hidden="true" />
          {ended
            ? t('executionLogs.ended')
            : hasOutput
              ? t('executionLogs.live')
              : t('executionLogs.waiting')}
        </div>
        <div className="execution-log-dialog__viewport" ref={viewportRef}>
          {query.isPending ? <p>{t('common.loading')}</p> : null}
          {query.isError ? <p role="alert">{t('executionLogs.loadFailed')}</p> : null}
          {!query.isPending && !query.isError && entries.length === 0 ? (
            <p className="execution-log-dialog__empty">{t('executionLogs.empty')}</p>
          ) : null}
          {entries.map((entry) => (
            <div
              className={`execution-log-line execution-log-line--${entry.level.toLowerCase()}`}
              key={entry.cursor}
            >
              <span className="execution-log-line__timestamp">
                {entry.timestamp ?? '—'}
              </span>
              <strong>{entry.level}</strong>
              <span className="execution-log-line__component">
                {[entry.component, entry.event].filter(Boolean).join(' · ') || 'worker'}
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
