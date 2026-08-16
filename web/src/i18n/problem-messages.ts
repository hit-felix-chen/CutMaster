import type { TFunction } from 'i18next'

import type { ProblemDetails } from '@/features/shared/api'

const safeProblemCode = /^[a-z][a-z0-9_]*$/

export function problemMessage(t: TFunction, problem: ProblemDetails | null) {
  const code = problem?.code
  if (!code || !safeProblemCode.test(code)) return t('common:errorTitle')
  const key = `problems:${code}`
  return t(key, {
    ...safeParameters(problem?.parameters),
    defaultValue: t('problems:generic'),
  })
}

export function problemBlockerMessage(t: TFunction, blocker: unknown) {
  if (!isRecord(blocker)) return t('problems:blockers.unknown')
  const type =
    typeof blocker.type === 'string'
      ? blocker.type
      : typeof blocker.kind === 'string'
        ? blocker.kind
        : 'unknown'
  const key = `problems:blockers.${safeProblemCode.test(type) ? type : 'unknown'}`
  return t(key, {
    ...safeParameters(blocker),
    ...safeParameters(blocker.metadata),
    id:
      blockerId(blocker) ||
      (isRecord(blocker.metadata) ? blockerId(blocker.metadata) : ''),
    defaultValue: t('problems:blockers.unknown'),
  })
}

export function problemFieldMessage(t: TFunction, issue: unknown) {
  if (!isRecord(issue)) return t('problems:fields.invalid')
  return t('problems:fields.invalidNamed', {
    field: typeof issue.field === 'string' ? issue.field : t('problems:fields.unknown'),
  })
}

function blockerId(value: Record<string, unknown>) {
  for (const key of [
    'project_id',
    'run_id',
    'render_variant_id',
    'attempt_id',
    'material_id',
    'reference',
  ]) {
    if (typeof value[key] === 'string') return value[key]
  }
  return ''
}

function safeParameters(value: unknown): Record<string, string | number | boolean> {
  if (!isRecord(value)) return {}
  return Object.fromEntries(
    Object.entries(value).filter(
      (entry): entry is [string, string | number | boolean] =>
        typeof entry[1] === 'string' ||
        typeof entry[1] === 'number' ||
        typeof entry[1] === 'boolean',
    ),
  )
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}
