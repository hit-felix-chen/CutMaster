import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertCircle, CheckCircle2, Clock3, Radio, Rows3, Square } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { ErrorState, LoadingState } from '@/components/ui/AsyncState'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { activityNavigationPath } from '@/features/activity/activity-route'
import { AsterProgress } from '@/features/shared/AsterProgress'
import { api, collectionItems, type ActivityItem } from '@/features/shared/api'
import { hasActiveActivity } from '@/features/shared/execution-state'

const groups = [
  {
    key: 'activity.running',
    statuses: ['running', 'retrying', 'stopping'],
    icon: Radio,
  },
  { key: 'activity.queued', statuses: ['queued'], icon: Clock3 },
  { key: 'activity.attention', statuses: ['failed', 'interrupted'], icon: AlertCircle },
  {
    key: 'activity.recent',
    statuses: ['complete', 'completed', 'reused'],
    icon: CheckCircle2,
  },
] as const

export function ActivityBoard() {
  const { t, i18n } = useTranslation('common')
  const activity = useQuery({
    queryKey: ['activity'],
    queryFn: api.activity.list,
    refetchInterval: (query) => {
      const value = query.state.data
      return value && hasActiveActivity(collectionItems(value)) ? 2000 : false
    },
  })
  if (activity.isPending)
    return (
      <div className="page">
        <LoadingState />
      </div>
    )
  if (activity.isError)
    return (
      <div className="page">
        <ErrorState onRetry={() => void activity.refetch()} />
      </div>
    )
  const attempts = collectionItems(activity.data)
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
        {groups.map(({ key, statuses, icon: Icon }) => {
          const values = attempts.filter(({ attempt }) =>
            statuses.includes(attempt.status.toLowerCase() as never),
          )
          if (values.length === 0) return null
          return (
            <section className="activity-group" key={key}>
              <header>
                <Icon size={17} />
                <h2>{t(key)}</h2>
                <span>{values.length}</span>
              </header>
              <div>
                {values.map((item) => (
                  <AttemptRow
                    key={item.attempt.attempt_id}
                    item={item}
                    locale={i18n.language}
                  />
                ))}
              </div>
            </section>
          )
        })}
      </div>
    </div>
  )
}

function AttemptRow({ item, locale }: { item: ActivityItem; locale: string }) {
  const { t } = useTranslation('common')
  const queryClient = useQueryClient()
  const { attempt } = item
  const stop = useMutation({
    mutationFn: () => api.attempts.stop(attempt.attempt_id),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['activity'] }),
        queryClient.invalidateQueries({ queryKey: ['project-runs'] }),
        attempt.owner_type === 'run'
          ? queryClient.invalidateQueries({ queryKey: ['run', attempt.owner_id] })
          : Promise.resolve(),
      ])
    },
  })
  const stoppable = ['queued', 'running', 'retrying'].includes(
    attempt.status.toLowerCase(),
  )
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
  return (
    <article className={`activity-row${destination ? ' activity-row--linked' : ''}`}>
      {destination ? (
        <Link
          className="activity-row__link"
          to={destination}
          aria-label={t('activity.openOwner', {
            owner: `${ownerLabel} ${attempt.owner_id}`,
          })}
        />
      ) : null}
      <div className="activity-row__identity">
        <strong>
          {operationKey[attempt.operation_type]
            ? t(operationKey[attempt.operation_type])
            : attempt.operation_type || t('activity.attempt')}
        </strong>
        <span>
          {ownerLabel} · {attempt.owner_id}
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
      {stoppable ? (
        <button
          className="button button--secondary activity-row__action"
          type="button"
          disabled={stop.isPending}
          onClick={() => stop.mutate()}
        >
          <Square size={13} />
          {t('activity.stopAttempt')}
        </button>
      ) : null}
      {attempt.error_message ? <p>{attempt.error_message}</p> : null}
      {showProgress ? (
        <AsterProgress compact execution={item} runStatus={attempt.status} />
      ) : null}
    </article>
  )
}
