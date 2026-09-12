import { WriteButton, WriteInput } from '@/components/ui/WriteControls'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  ArrowRight,
  CheckCircle2,
  DatabaseBackup,
  FolderInput,
  LoaderCircle,
  ShieldAlert,
  XCircle,
} from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { OperationProblem } from '@/components/ui/OperationProblem'
import {
  api,
  type DataRootMigration,
  type DataRootMigrationBlocker,
  type DataRootMigrationStatus,
} from '@/features/shared/api'
import {
  isDataRootMigrationActive,
  isDataRootMigrationBlocking,
  useCurrentDataRootMigration,
} from '@/features/settings/data-root-migration'
import { formatBytes, formatNumber } from '@/i18n/formatters'

const cancellableStatuses = new Set<DataRootMigrationStatus>([
  'requested',
  'quiescing',
  'copying',
  'verifying',
])

function isAbsolutePath(value: string) {
  return (
    value.startsWith('/') || /^[A-Za-z]:[\\/]/.test(value) || value.startsWith('\\\\')
  )
}

function blockerLabel(
  blocker: DataRootMigrationBlocker,
  t: ReturnType<typeof useTranslation<'common'>>['t'],
) {
  const kind = /^[a-z][a-z0-9_]*$/.test(blocker.kind) ? blocker.kind : 'unknown'
  const metadata = Object.fromEntries(
    Object.entries(blocker.metadata).filter(
      (entry): entry is [string, string | number | boolean] =>
        typeof entry[1] === 'string' ||
        typeof entry[1] === 'number' ||
        typeof entry[1] === 'boolean',
    ),
  )
  return t(`settings.dataRootMigration.blockers.${kind}`, {
    ...metadata,
    defaultValue: t('settings.dataRootMigration.blockers.unknown'),
  })
}

function failureLabel(
  code: string,
  t: ReturnType<typeof useTranslation<'common'>>['t'],
) {
  const safeCode = /^[a-z][a-z0-9_]*$/.test(code) ? code : 'unknown'
  return t(`settings.dataRootMigration.failures.${safeCode}`, {
    defaultValue: t('settings.dataRootMigration.failures.unknown'),
  })
}

function boundedMetric(completedValue: number, totalValue: number) {
  const completed = Number.isFinite(completedValue) ? Math.max(0, completedValue) : 0
  const total = Number.isFinite(totalValue) ? Math.max(0, totalValue) : 0
  return { completed: total > 0 ? Math.min(completed, total) : completed, total }
}

function MigrationProgress({ migration }: { migration: DataRootMigration }) {
  const { t, i18n } = useTranslation('common')
  const files = boundedMetric(
    migration.progress.files_completed,
    migration.progress.files_total,
  )
  const bytes = boundedMetric(
    migration.progress.bytes_completed,
    migration.progress.bytes_total,
  )
  const metrics = [
    {
      key: 'files',
      label: t('settings.dataRootMigration.files'),
      completed: files.completed,
      total: files.total,
      displayCompleted: formatNumber(files.completed, i18n.language),
      displayTotal: formatNumber(files.total, i18n.language),
    },
    {
      key: 'bytes',
      label: t('settings.dataRootMigration.bytes'),
      completed: bytes.completed,
      total: bytes.total,
      displayCompleted: formatBytes(bytes.completed, i18n.language),
      displayTotal: formatBytes(bytes.total, i18n.language),
    },
  ]
  return (
    <div className="data-root-migration__progress" aria-live="polite">
      <div className="data-root-migration__phase">
        <LoaderCircle
          className={isDataRootMigrationActive(migration) ? 'spin' : undefined}
          size={16}
          aria-hidden="true"
        />
        <span>
          {t(`settings.dataRootMigration.phases.${migration.progress.phase}`, {
            defaultValue: migration.progress.phase,
          })}
        </span>
      </div>
      {metrics.map((metric) => (
        <div className="migration-progress-metric" key={metric.key}>
          <div>
            <span>{metric.label}</span>
            <strong>
              {metric.total > 0
                ? t('settings.dataRootMigration.progressKnown', {
                    completed: metric.displayCompleted,
                    total: metric.displayTotal,
                  })
                : t('settings.dataRootMigration.progressUnknown', {
                    completed: metric.displayCompleted,
                  })}
            </strong>
          </div>
          {metric.total > 0 ? (
            <div
              className="migration-progress-track"
              role="progressbar"
              aria-label={metric.label}
              aria-valuemin={0}
              aria-valuemax={metric.total}
              aria-valuenow={metric.completed}
            >
              <span style={{ width: `${(metric.completed / metric.total) * 100}%` }} />
            </div>
          ) : (
            <small>{t('settings.dataRootMigration.totalPending')}</small>
          )}
        </div>
      ))}
    </div>
  )
}

function BlockerList({ blockers }: { blockers: DataRootMigrationBlocker[] }) {
  const { t } = useTranslation('common')
  if (!blockers.length) return null
  return (
    <div className="data-root-migration__blockers" role="alert">
      <strong>{t('settings.dataRootMigration.blockedTitle')}</strong>
      <ul>
        {blockers.map((blocker, index) => (
          <li key={`${blocker.kind}-${index}`}>
            <ShieldAlert size={15} aria-hidden="true" />
            <span>{blockerLabel(blocker, t)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

function MigrationSummary({ migration }: { migration: DataRootMigration }) {
  const { t } = useTranslation('common')
  const terminalIcon =
    migration.status === 'failed' || migration.status === 'cancelled' ? (
      <XCircle size={18} aria-hidden="true" />
    ) : migration.status === 'complete' ? (
      <CheckCircle2 size={18} aria-hidden="true" />
    ) : (
      <DatabaseBackup size={18} aria-hidden="true" />
    )
  return (
    <div
      className={`data-root-migration__current data-root-migration__current--${migration.status}`}
    >
      <header>
        {terminalIcon}
        <div>
          <span>{t('settings.dataRootMigration.current')}</span>
          <strong>
            {t(`settings.dataRootMigration.statuses.${migration.status}`)}
          </strong>
        </div>
      </header>
      <div className="data-root-migration__route">
        <code>{migration.source_root}</code>
        <ArrowRight size={16} aria-hidden="true" />
        <code>{migration.destination_root}</code>
      </div>
      {isDataRootMigrationActive(migration) ? (
        <MigrationProgress migration={migration} />
      ) : null}
      {migration.status === 'restart_required' ? (
        <div className="data-root-migration__restart" role="alert">
          <strong>{t('settings.dataRootMigration.restartTitle')}</strong>
          <p>{t('settings.dataRootMigration.restartBody')}</p>
          <p>{t('settings.dataRootMigration.sourceRetained')}</p>
        </div>
      ) : null}
      {migration.failure ? (
        <div className="data-root-migration__failure" role="alert">
          <strong>{t('settings.dataRootMigration.failureTitle')}</strong>
          <p>{failureLabel(migration.failure.code, t)}</p>
        </div>
      ) : null}
      <BlockerList blockers={migration.blockers} />
    </div>
  )
}

export function DataRootMigrationPanel({
  applicationBlocked = false,
}: {
  applicationBlocked?: boolean
}) {
  const { t, i18n } = useTranslation('common')
  const queryClient = useQueryClient()
  const current = useCurrentDataRootMigration()
  const [destination, setDestination] = useState('')
  const [preflightInput, setPreflightInput] = useState<string | null>(null)
  const [validationAttempted, setValidationAttempted] = useState(false)
  const [armedDestination, setArmedDestination] = useState<string | null>(null)
  const normalizedInput = destination.trim()
  const validPath = isAbsolutePath(normalizedInput)
  const preflight = useMutation({
    mutationFn: api.settings.preflightDataRootMigration,
  })
  const start = useMutation({
    mutationFn: api.settings.startDataRootMigration,
    onSuccess: async (migration) => {
      queryClient.setQueryData(['settings-storage-migration-current'], {
        migration,
      })
      await queryClient.invalidateQueries({ queryKey: ['health'] })
    },
  })
  const cancel = useMutation({
    mutationFn: api.settings.cancelDataRootMigration,
    onSuccess: async (migration) => {
      queryClient.setQueryData(['settings-storage-migration-current'], {
        migration,
      })
      await queryClient.invalidateQueries({ queryKey: ['health'] })
    },
  })
  const applicablePreflight =
    preflightInput === normalizedInput ? preflight.data : undefined
  const migration = current.data?.migration
  const migrationBlocksNew =
    applicationBlocked || isDataRootMigrationBlocking(migration)
  const canCancel = Boolean(
    migration &&
    cancellableStatuses.has(migration.status) &&
    !migration.cancel_requested,
  )

  const runPreflight = () => {
    setValidationAttempted(true)
    setArmedDestination(null)
    if (!validPath) return
    setPreflightInput(normalizedInput)
    preflight.mutate(normalizedInput)
  }
  const startMigration = () => {
    if (!applicablePreflight?.eligible || start.isPending) return
    if (armedDestination !== applicablePreflight.destination_root) {
      setArmedDestination(applicablePreflight.destination_root)
      return
    }
    start.mutate(applicablePreflight.destination_root)
  }

  return (
    <div className="data-root-migration">
      <header>
        <DatabaseBackup size={18} aria-hidden="true" />
        <div>
          <h3>{t('settings.dataRootMigration.title')}</h3>
          <p>{t('settings.dataRootMigration.description')}</p>
        </div>
      </header>
      {current.isPending ? <LoaderCircle className="spin" size={18} /> : null}
      {current.isError ? <OperationProblem error={current.error} /> : null}
      {migration ? <MigrationSummary migration={migration} /> : null}
      {migration && canCancel ? (
        <WriteButton
          className="button button--secondary"
          type="button"
          disabled={cancel.isPending}
          onClick={() => cancel.mutate(migration.migration_id)}
        >
          {cancel.isPending ? (
            <LoaderCircle className="spin" size={15} aria-hidden="true" />
          ) : (
            <XCircle size={15} aria-hidden="true" />
          )}
          {t('settings.dataRootMigration.cancel')}
        </WriteButton>
      ) : null}
      {cancel.isError ? <OperationProblem error={cancel.error} /> : null}
      {!migrationBlocksNew && current.isSuccess ? (
        <div className="data-root-migration__form">
          <label htmlFor="data-root-destination">
            <span>{t('settings.dataRootMigration.destination')}</span>
            <div className="data-root-migration__input">
              <FolderInput size={16} aria-hidden="true" />
              <WriteInput
                id="data-root-destination"
                type="text"
                value={destination}
                placeholder={t('settings.dataRootMigration.destinationPlaceholder')}
                aria-invalid={validationAttempted && !validPath}
                onChange={(event) => {
                  setDestination(event.target.value)
                  setArmedDestination(null)
                }}
              />
            </div>
          </label>
          {validationAttempted && !validPath ? (
            <p className="field-error" role="alert">
              {t('settings.dataRootMigration.absolutePathRequired')}
            </p>
          ) : null}
          <WriteButton
            className="button button--secondary"
            type="button"
            disabled={preflight.isPending || !normalizedInput}
            onClick={runPreflight}
          >
            {preflight.isPending ? (
              <LoaderCircle className="spin" size={15} aria-hidden="true" />
            ) : (
              <ShieldAlert size={15} aria-hidden="true" />
            )}
            {t('settings.dataRootMigration.preflight')}
          </WriteButton>
          {preflight.isError ? <OperationProblem error={preflight.error} /> : null}
          {applicablePreflight ? (
            <div className="data-root-migration__preflight" aria-live="polite">
              <div className="data-root-migration__route">
                <code>{applicablePreflight.source_root}</code>
                <ArrowRight size={16} aria-hidden="true" />
                <code>{applicablePreflight.destination_root}</code>
              </div>
              <dl>
                <div>
                  <dt>{t('settings.dataRootMigration.estimatedFiles')}</dt>
                  <dd>
                    {formatNumber(
                      applicablePreflight.estimated_file_count,
                      i18n.language,
                    )}
                  </dd>
                </div>
                <div>
                  <dt>{t('settings.dataRootMigration.estimatedSize')}</dt>
                  <dd>
                    {formatBytes(
                      applicablePreflight.estimated_size_bytes,
                      i18n.language,
                    )}
                  </dd>
                </div>
              </dl>
              <BlockerList blockers={applicablePreflight.blockers} />
              {applicablePreflight.eligible ? (
                <div className="data-root-migration__start">
                  <p>{t('settings.dataRootMigration.startWarning')}</p>
                  <WriteButton
                    className="button button--primary"
                    type="button"
                    disabled={start.isPending}
                    onClick={startMigration}
                  >
                    {start.isPending ? (
                      <LoaderCircle className="spin" size={15} aria-hidden="true" />
                    ) : (
                      <DatabaseBackup size={15} aria-hidden="true" />
                    )}
                    {armedDestination === applicablePreflight.destination_root
                      ? t('settings.dataRootMigration.confirmStart')
                      : t('settings.dataRootMigration.start')}
                  </WriteButton>
                </div>
              ) : null}
            </div>
          ) : null}
          {start.isError ? <OperationProblem error={start.error} /> : null}
        </div>
      ) : null}
    </div>
  )
}
