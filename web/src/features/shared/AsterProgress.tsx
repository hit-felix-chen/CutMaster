import { Check, Clock3, LoaderCircle } from 'lucide-react'
import { useTranslation } from 'react-i18next'

import type {
  AsterAgent,
  AsterMilestoneState,
  ExecutionSummary,
} from '@/features/shared/api'
import { isJobProgress } from '@/features/shared/execution-state'

const agents = [
  ['A', 'arrangement_architect'],
  ['S', 'story_editor'],
  ['T', 'timeline_scout'],
  ['E', 'edit_composer'],
] as const satisfies ReadonlyArray<readonly [string, AsterAgent]>

const agentKey: Record<AsterAgent, string> = {
  arrangement_architect: 'arrangementArchitect',
  story_editor: 'storyEditor',
  timeline_scout: 'timelineScout',
  edit_composer: 'editComposer',
  revision_editor: 'legacyRevisionEditor',
}

interface AsterProgressProps {
  execution: ExecutionSummary | null
  runStatus: string
  compact?: boolean
}

export function AsterProgress({
  execution,
  runStatus,
  compact = false,
}: AsterProgressProps) {
  const { t, i18n } = useTranslation('common')
  const rawProgress = execution?.job.progress ?? {}
  const progress = isJobProgress(rawProgress) ? rawProgress : null
  const runComplete = ['complete', 'completed'].includes(runStatus.toLowerCase())
  const legacyActive =
    !runComplete &&
    (runStatus.toLowerCase() === 'planners' ||
      ['running', 'retrying', 'stopping'].includes(
        execution?.attempt.status.toLowerCase() ?? '',
      ))
  const milestoneStates = new Map<AsterAgent, AsterMilestoneState>(
    progress
      ? progress.milestones.map((milestone) => [milestone.agent, milestone.state])
      : [],
  )
  const currentAgent = runComplete ? null : progress?.agent
  const currentKey =
    currentAgent && currentAgent !== 'revision_editor' ? agentKey[currentAgent] : null
  const preparing = progress?.state === 'preparing'
  const heartbeat = formatHeartbeat(execution?.job.heartbeat_at, i18n.language)

  return (
    <section
      className={`aster-progress${compact ? ' aster-progress--compact' : ''}`}
      aria-label={t('progress.title')}
    >
      <header className="aster-progress__header">
        <div>
          <span className="eyebrow">ASTER</span>
          <strong>
            {runComplete
              ? t('progress.complete')
              : currentKey
                ? t(`progress.agents.${currentKey}.active`)
                : preparing
                  ? t('progress.preparing')
                  : legacyActive
                    ? t('projects.asterWorking')
                    : t('progress.preparing')}
          </strong>
          {!compact ? (
            <p>
              {runComplete
                ? t('progress.completeBody')
                : currentKey
                  ? t(`progress.agents.${currentKey}.description`)
                  : preparing
                    ? t('progress.preparingBody')
                    : legacyActive
                      ? t('progress.legacyWorkingBody')
                      : t('progress.preparingBody')}
            </p>
          ) : null}
        </div>
        {heartbeat ? (
          <span
            className="aster-progress__heartbeat"
            title={execution?.job.heartbeat_at ?? undefined}
          >
            <span aria-hidden="true" />
            {t('progress.heartbeat', { time: heartbeat })}
          </span>
        ) : null}
      </header>
      <ol className="aster-progress__milestones">
        {agents.map(([letter, agent]) => {
          const state = runComplete
            ? 'complete'
            : (milestoneStates.get(agent) ?? 'queued')
          const key = agentKey[agent]
          return (
            <li key={agent} data-state={state}>
              <span className="aster-progress__letter">{letter}</span>
              <span className="aster-progress__agent">
                <strong>{t(`progress.agents.${key}.name`)}</strong>
                {!compact ? <small>{t(`activity.${state}`)}</small> : null}
              </span>
              <MilestoneIcon state={state} />
            </li>
          )
        })}
      </ol>
      {!compact ? (
        <p className="aster-progress__renderer">{t('progress.rendererHandoff')}</p>
      ) : null}
    </section>
  )
}

function MilestoneIcon({ state }: { state: AsterMilestoneState }) {
  if (state === 'complete') return <Check size={13} aria-hidden="true" />
  if (state === 'running') {
    return <LoaderCircle className="spin" size={13} aria-hidden="true" />
  }
  return <Clock3 size={13} aria-hidden="true" />
}

function formatHeartbeat(value: string | null | undefined, locale: string) {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return new Intl.DateTimeFormat(locale, {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  }).format(date)
}
