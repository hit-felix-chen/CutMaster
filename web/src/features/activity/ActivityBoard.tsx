import {
  useInfiniteQuery,
  useMutation,
  useQueryClient,
  type QueryClient,
} from '@tanstack/react-query'
import {
  AlertCircle,
  CheckCircle2,
  Clock3,
  LoaderCircle,
  ListChecks,
  Play,
  Radio,
  RefreshCcw,
  Rows3,
  Square,
  SquareCheckBig,
  Trash2,
  X,
} from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { useEventStream } from '@/app/providers/event-stream-context'
import { ErrorState, LoadingState } from '@/components/ui/AsyncState'
import { OperationProblem } from '@/components/ui/OperationProblem'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { activityNavigationPath } from '@/features/activity/activity-route'
import { AsterProgress } from '@/features/shared/AsterProgress'
import { ExecutionFailure } from '@/features/shared/ExecutionFailure'
import { api, type ActivityItem } from '@/features/shared/api'
import { hasActiveActivity } from '@/features/shared/execution-state'

const groups = [
  {
    id: 'running',
    key: 'activity.running',
    statuses: ['running', 'retrying', 'stopping'],
    icon: Radio,
  },
  { id: 'queued', key: 'activity.queued', statuses: ['queued'], icon: Clock3 },
  {
    id: 'attention',
    key: 'activity.attention',
    statuses: ['failed', 'interrupted'],
    icon: AlertCircle,
    manageable: true,
  },
  {
    id: 'recent',
    key: 'activity.recent',
    statuses: ['complete', 'completed', 'reused'],
    icon: CheckCircle2,
    manageable: true,
  },
] as const

type ManageableGroup = 'attention' | 'recent'

export function ActivityBoard() {
  const { t, i18n } = useTranslation('common')
  const { isConnected } = useEventStream()
  const queryClient = useQueryClient()
  const [managingGroup, setManagingGroup] = useState<ManageableGroup | null>(null)
  const [selectedAttemptIds, setSelectedAttemptIds] = useState<Set<string>>(
    () => new Set(),
  )
  const dismiss = useMutation({
    mutationFn: (attemptIds: string[]) => api.activity.dismiss(attemptIds),
    onSuccess: async () => {
      setSelectedAttemptIds(new Set())
      setManagingGroup(null)
      await queryClient.invalidateQueries({ queryKey: ['activity'] })
    },
  })
  const activity = useInfiniteQuery({
    queryKey: ['activity'],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => api.activity.list(pageParam),
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? lastPage.offset + lastPage.limit : undefined,
    refetchInterval: (query) => {
      if (isConnected) return false
      const values = query.state.data?.pages.flatMap((page) => page.items) ?? []
      return hasActiveActivity(values) ? 2000 : false
    },
  })
  if (activity.isPending)
    return (
      <div className="page">
        <LoadingState />
      </div>
    )
  if (activity.isError && !activity.data)
    return (
      <div className="page">
        <ErrorState onRetry={() => void activity.refetch()} />
      </div>
    )
  if (!activity.data) return null
  const attempts = activity.data.pages.flatMap((page) => page.items)
  return (
    <div className="page">
      <header className="page-header">
        <div>
          <span className="eyebrow">{t('activity.executionEyebrow')}</span>
          <h1>{t('activity.title')}</h1>
          <p>{t('activity.subtitle')}</p>
        </div>
      </header>
      {attempts.length === 0 ? (
        <section className="empty-panel">
          <Rows3 size={29} />
          <h2>{t('activity.empty')}</h2>
        </section>
      ) : null}
      <div className="activity-groups">
        {groups.map(({ id, key, statuses, icon: Icon, ...group }) => {
          const values = attempts.filter(({ attempt }) =>
            statuses.includes(attempt.status.toLowerCase() as never),
          )
          if (values.length === 0) return null
          const manageable = 'manageable' in group && group.manageable
          const isManaging = manageable && managingGroup === id
          const valueIds = values.map(({ attempt }) => attempt.attempt_id)
          const allSelected =
            valueIds.length > 0 &&
            valueIds.every((attemptId) => selectedAttemptIds.has(attemptId))
          return (
            <section className="activity-group" key={key}>
              <header>
                <Icon size={17} />
                <h2>{t(key)}</h2>
                <span className="activity-group__count">{values.length}</span>
                {manageable ? (
                  <div className="activity-group__management">
                    {isManaging ? (
                      <>
                        <button
                          className="button button--secondary"
                          type="button"
                          onClick={() => {
                            setSelectedAttemptIds((current) => {
                              const next = new Set(current)
                              if (allSelected) {
                                valueIds.forEach((attemptId) => next.delete(attemptId))
                              } else {
                                valueIds.forEach((attemptId) => next.add(attemptId))
                              }
                              return next
                            })
                          }}
                        >
                          <SquareCheckBig size={14} aria-hidden="true" />
                          {t(
                            allSelected
                              ? 'activity.clearSelection'
                              : 'activity.selectAll',
                          )}
                        </button>
                        <button
                          className="button button--danger"
                          type="button"
                          disabled={selectedAttemptIds.size === 0 || dismiss.isPending}
                          onClick={() => dismiss.mutate([...selectedAttemptIds])}
                        >
                          {dismiss.isPending ? (
                            <LoaderCircle
                              className="spin"
                              size={14}
                              aria-hidden="true"
                            />
                          ) : (
                            <Trash2 size={14} aria-hidden="true" />
                          )}
                          {t('activity.clearSelected', {
                            count: selectedAttemptIds.size,
                          })}
                        </button>
                        <button
                          className="button button--secondary"
                          type="button"
                          disabled={dismiss.isPending}
                          onClick={() => {
                            setSelectedAttemptIds(new Set())
                            setManagingGroup(null)
                            dismiss.reset()
                          }}
                        >
                          <X size={14} aria-hidden="true" />
                          {t('activity.doneManaging')}
                        </button>
                      </>
                    ) : (
                      <button
                        className="button button--secondary"
                        type="button"
                        onClick={() => {
                          setSelectedAttemptIds(new Set())
                          setManagingGroup(id as ManageableGroup)
                          dismiss.reset()
                        }}
                      >
                        <ListChecks size={14} aria-hidden="true" />
                        {t('activity.manage')}
                      </button>
                    )}
                  </div>
                ) : null}
              </header>
              <div>
                {values.map((item) => (
                  <AttemptRow
                    key={item.attempt.attempt_id}
                    item={item}
                    locale={i18n.language}
                    managing={isManaging}
                    selected={selectedAttemptIds.has(item.attempt.attempt_id)}
                    onSelectionChange={(selected) => {
                      setSelectedAttemptIds((current) => {
                        const next = new Set(current)
                        if (selected) next.add(item.attempt.attempt_id)
                        else next.delete(item.attempt.attempt_id)
                        return next
                      })
                    }}
                  />
                ))}
              </div>
              {isManaging && dismiss.isError ? (
                <OperationProblem error={dismiss.error} />
              ) : null}
            </section>
          )
        })}
      </div>
      {activity.hasNextPage ? (
        <div className="activity-load-more">
          <button
            className="button button--secondary"
            type="button"
            disabled={activity.isFetchingNextPage}
            onClick={() => void activity.fetchNextPage()}
          >
            {activity.isFetchingNextPage ? (
              <LoaderCircle className="spin" size={15} aria-hidden="true" />
            ) : null}
            {t(
              activity.isFetchingNextPage
                ? 'activity.loadingMore'
                : 'activity.loadMore',
            )}
          </button>
        </div>
      ) : null}
      {activity.isFetchNextPageError ? (
        <OperationProblem error={activity.error} />
      ) : null}
    </div>
  )
}

type RecoveryAction = 'retry' | 'resume'

async function recoverActivityItem(
  item: ActivityItem,
  action: RecoveryAction,
): Promise<unknown> {
  const { owner_id: ownerId, owner_type: ownerType } = item.attempt
  if (ownerType === 'material') return api.materials[action](ownerId)
  if (ownerType === 'run') return api.runs[action](ownerId)
  if (ownerType === 'render_variant') return api.renderVariants[action](ownerId)
  throw new Error(`Unsupported recovery owner: ${ownerType}`)
}

async function refreshActivityItem(queryClient: QueryClient, item: ActivityItem) {
  const requests = [queryClient.invalidateQueries({ queryKey: ['activity'] })]
  const { navigation } = item
  if (navigation?.type === 'material') {
    requests.push(
      queryClient.invalidateQueries({ queryKey: ['materials'] }),
      queryClient.invalidateQueries({
        queryKey: ['material', navigation.material_id],
      }),
    )
  } else if (navigation?.type === 'run') {
    requests.push(
      queryClient.invalidateQueries({ queryKey: ['run', navigation.run_id] }),
      queryClient.invalidateQueries({
        queryKey: ['project-runs', navigation.project_id],
      }),
      queryClient.invalidateQueries({
        queryKey: ['project-workspace', navigation.project_id],
      }),
      queryClient.invalidateQueries({ queryKey: ['projects'] }),
    )
  } else if (navigation?.type === 'render_variant') {
    requests.push(
      queryClient.invalidateQueries({
        queryKey: ['render-variants', 'edit', navigation.edit_id],
      }),
      queryClient.invalidateQueries({
        queryKey: ['project-render-variants', navigation.project_id],
      }),
      queryClient.invalidateQueries({
        queryKey: ['frozen-edit-review', navigation.edit_id],
      }),
      queryClient.invalidateQueries({
        queryKey: ['project-workspace', navigation.project_id],
      }),
    )
  } else if (item.attempt.owner_type === 'material') {
    requests.push(
      queryClient.invalidateQueries({ queryKey: ['materials'] }),
      queryClient.invalidateQueries({
        queryKey: ['material', item.attempt.owner_id],
      }),
    )
  } else if (item.attempt.owner_type === 'run') {
    requests.push(
      queryClient.invalidateQueries({ queryKey: ['project-runs'] }),
      queryClient.invalidateQueries({
        queryKey: ['run', item.attempt.owner_id],
      }),
    )
  } else if (item.attempt.owner_type === 'render_variant') {
    requests.push(
      queryClient.invalidateQueries({ queryKey: ['render-variants'] }),
      queryClient.invalidateQueries({ queryKey: ['project-render-variants'] }),
    )
  }
  await Promise.all(requests)
}

function AttemptRow({
  item,
  locale,
  managing = false,
  selected = false,
  onSelectionChange,
}: {
  item: ActivityItem
  locale: string
  managing?: boolean
  selected?: boolean
  onSelectionChange?: (selected: boolean) => void
}) {
  const { t } = useTranslation('common')
  const queryClient = useQueryClient()
  const { attempt } = item
  const stop = useMutation({
    mutationFn: () => api.attempts.stop(attempt.attempt_id),
    onSuccess: () => refreshActivityItem(queryClient, item),
  })
  const normalizedStatus = attempt.status.toLowerCase()
  const recoveryAction: RecoveryAction | null =
    normalizedStatus === 'failed'
      ? 'retry'
      : normalizedStatus === 'interrupted'
        ? 'resume'
        : null
  const recoverySupported = ['material', 'run', 'render_variant'].includes(
    attempt.owner_type,
  )
  const recovery = useMutation({
    mutationFn: (action: RecoveryAction) => recoverActivityItem(item, action),
    onSuccess: () => refreshActivityItem(queryClient, item),
  })
  const stoppable = ['queued', 'running', 'retrying'].includes(normalizedStatus)
  const operationKey: Record<string, string> = {
    material_analysis: 'activity.materialAnalysis',
    aster_planning: 'activity.asterPlanning',
    rendering: 'activity.rendering',
  }
  const ownerKey: Record<string, string> = {
    material: 'activity.materialOwner',
    run: 'activity.runOwner',
    render_variant: 'activity.renderVariantOwner',
  }
  const showProgress =
    attempt.operation_type === 'aster_planning' &&
    ['queued', 'running', 'retrying', 'stopping'].includes(attempt.status.toLowerCase())
  const ownerLabel = ownerKey[attempt.owner_type]
    ? t(ownerKey[attempt.owner_type])
    : attempt.owner_type
  const destination = item.navigation ? activityNavigationPath(item.navigation) : null
  const ownerContext =
    item.navigation?.type === 'material'
      ? item.navigation.material_name
      : item.navigation?.type === 'run'
        ? t('activity.runContext', {
            project: item.navigation.project_name,
            sequence: item.navigation.run_sequence,
          })
        : item.navigation?.type === 'render_variant'
          ? t('activity.renderContext', {
              project: item.navigation.project_name,
              runSequence: item.navigation.run_sequence,
              editVersion: item.navigation.edit_version,
              audioMode: t(`renders.audioMode.${item.navigation.audio_mode}`),
            })
          : t('activity.ownerUnavailable')
  return (
    <article
      className={`activity-row${destination && !managing ? ' activity-row--linked' : ''}${managing ? ' activity-row--managing' : ''}`}
    >
      {destination && !managing ? (
        <Link
          className="activity-row__link"
          to={destination}
          aria-label={t('activity.openOwner', {
            owner: `${ownerLabel} · ${ownerContext}`,
          })}
        />
      ) : null}
      {managing ? (
        <label className="activity-row__selector">
          <input
            type="checkbox"
            checked={selected}
            onChange={(event) => onSelectionChange?.(event.target.checked)}
            aria-label={t('activity.selectAttempt', {
              attempt: `${ownerLabel} · ${ownerContext}`,
            })}
          />
        </label>
      ) : null}
      <div className="activity-row__identity">
        <strong>
          {operationKey[attempt.operation_type]
            ? t(operationKey[attempt.operation_type])
            : attempt.operation_type || t('activity.attempt')}
        </strong>
        <span>
          {ownerLabel} · {ownerContext}
        </span>
      </div>
      <span className="activity-row__sequence">#{attempt.sequence}</span>
      <StatusBadge status={attempt.status} />
      <time dateTime={attempt.updated_at}>
        {new Intl.DateTimeFormat(locale, {
          dateStyle: 'medium',
          timeStyle: 'short',
        }).format(new Date(attempt.updated_at))}
      </time>
      {stoppable && !managing ? (
        <button
          className="button button--secondary activity-row__action"
          type="button"
          disabled={stop.isPending || recovery.isPending}
          onClick={() => {
            recovery.reset()
            stop.mutate()
          }}
        >
          {stop.isPending ? (
            <LoaderCircle className="spin" size={13} aria-hidden="true" />
          ) : (
            <Square size={13} aria-hidden="true" />
          )}
          {t('activity.stopAttempt')}
        </button>
      ) : null}
      {recoveryAction && recoverySupported && !managing ? (
        <button
          className="button button--secondary activity-row__action"
          type="button"
          disabled={recovery.isPending || stop.isPending}
          onClick={() => {
            stop.reset()
            recovery.mutate(recoveryAction)
          }}
        >
          {recovery.isPending ? (
            <LoaderCircle className="spin" size={13} aria-hidden="true" />
          ) : recoveryAction === 'retry' ? (
            <RefreshCcw size={13} aria-hidden="true" />
          ) : (
            <Play size={13} aria-hidden="true" />
          )}
          {t(
            recoveryAction === 'retry'
              ? 'activity.retryAttempt'
              : 'activity.resumeAttempt',
          )}
        </button>
      ) : null}
      {attempt.error_message ? (
        <ExecutionFailure
          operationType={attempt.operation_type}
          ownerType={attempt.owner_type}
          status={attempt.status}
        />
      ) : null}
      {stop.isError ? <OperationProblem error={stop.error} /> : null}
      {recovery.isError ? <OperationProblem error={recovery.error} /> : null}
      {showProgress ? (
        <AsterProgress compact execution={item} runStatus={attempt.status} />
      ) : null}
    </article>
  )
}
