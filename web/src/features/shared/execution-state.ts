import type {
  ActivityItem,
  ExecutionSummary,
  JobProgress,
  RunListItem,
} from '@/features/shared/api'

const terminalRunStatuses = new Set(['complete', 'completed', 'failed', 'interrupted'])
const activeAttemptStatuses = new Set(['queued', 'running', 'retrying', 'stopping'])
const asterAgents = new Set([
  'arrangement_architect',
  'story_editor',
  'timeline_scout',
  'edit_composer',
  'revision_editor',
])
const milestoneStates = new Set(['queued', 'running', 'complete'])

export function executionStatus(
  runStatus: string,
  execution: ExecutionSummary | null,
): string {
  const normalizedRun = runStatus.trim().toLowerCase()
  if (terminalRunStatuses.has(normalizedRun)) return normalizedRun
  const attemptStatus = execution?.attempt.status.trim().toLowerCase()
  if (attemptStatus) return attemptStatus
  return normalizedRun === 'planners' ? 'running' : normalizedRun
}

export function isRunExecutionActive(
  runStatus: string,
  execution: ExecutionSummary | null,
): boolean {
  const normalizedRun = runStatus.trim().toLowerCase()
  if (terminalRunStatuses.has(normalizedRun)) return false
  if (normalizedRun === 'queued' || normalizedRun === 'planners') return true
  const attemptStatus = execution?.attempt.status.trim().toLowerCase()
  return Boolean(attemptStatus && activeAttemptStatuses.has(attemptStatus))
}

export function hasActiveRun(items: RunListItem[]): boolean {
  return items.some(({ run, execution }) => isRunExecutionActive(run.status, execution))
}

export function hasActiveActivity(items: ActivityItem[]): boolean {
  return items.some(({ attempt }) =>
    activeAttemptStatuses.has(attempt.status.trim().toLowerCase()),
  )
}

export function isJobProgress(value: unknown): value is JobProgress {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return false
  }
  const record = value as Record<string, unknown>
  const milestones = record.milestones
  const validStateAndAgent =
    (record.state === 'preparing' && record.agent === null) ||
    ((record.state === 'running' || record.state === 'complete') &&
      typeof record.agent === 'string' &&
      asterAgents.has(record.agent))
  return (
    record.schema_version === '1.0' &&
    record.phase === 'planners' &&
    validStateAndAgent &&
    typeof record.completed === 'number' &&
    Number.isInteger(record.completed) &&
    record.completed >= 0 &&
    record.completed <= 5 &&
    (record.total === 4 || record.total === 5) &&
    record.completed <= record.total &&
    record.unit === 'agent' &&
    Array.isArray(milestones) &&
    milestones.every((milestone) => {
      if (
        typeof milestone !== 'object' ||
        milestone === null ||
        Array.isArray(milestone)
      ) {
        return false
      }
      const item = milestone as Record<string, unknown>
      return (
        typeof item.id === 'string' &&
        typeof item.agent === 'string' &&
        asterAgents.has(item.agent) &&
        typeof item.state === 'string' &&
        milestoneStates.has(item.state)
      )
    })
  )
}
