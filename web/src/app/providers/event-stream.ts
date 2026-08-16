import type { Query, QueryClient } from '@tanstack/react-query'

export interface DurableEvent {
  event_id: number
  event_type: string
  occurred_at: string
  object_type: string
  object_id: string
  command_id: string | null
  attempt_id: string | null
  job_id: string | null
  payload: Record<string, never>
  schema_version: '1.0'
}

const authoritativeRoots = new Set([
  'activity',
  'frozen-edit-review',
  'health',
  'material',
  'material-memory',
  'materials',
  'project-render-variants',
  'project-runs',
  'project-workspace',
  'projects',
  'render-variant',
  'render-variants',
  'run',
  'settings',
  'settings-storage',
  'settings-storage-migration-current',
])

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function nullableString(value: unknown): value is string | null {
  return value === null || typeof value === 'string'
}

export function parseDurableEvent(value: string): DurableEvent | null {
  let decoded: unknown
  try {
    decoded = JSON.parse(value)
  } catch {
    return null
  }
  if (
    !isRecord(decoded) ||
    !isRecord(decoded.payload) ||
    Object.keys(decoded.payload).length !== 0
  ) {
    return null
  }
  if (
    !Number.isSafeInteger(decoded.event_id) ||
    Number(decoded.event_id) <= 0 ||
    typeof decoded.event_type !== 'string' ||
    !decoded.event_type ||
    typeof decoded.occurred_at !== 'string' ||
    !decoded.occurred_at ||
    typeof decoded.object_type !== 'string' ||
    !decoded.object_type ||
    typeof decoded.object_id !== 'string' ||
    !decoded.object_id ||
    !nullableString(decoded.command_id) ||
    !nullableString(decoded.attempt_id) ||
    !nullableString(decoded.job_id) ||
    decoded.schema_version !== '1.0'
  ) {
    return null
  }
  return decoded as unknown as DurableEvent
}

function hasObjectReference(
  value: unknown,
  field: string,
  objectId: string,
  visited = new WeakSet<object>(),
): boolean {
  if (!value || typeof value !== 'object') return false
  if (visited.has(value)) return false
  visited.add(value)
  if (Array.isArray(value)) {
    return value.some((item) => hasObjectReference(item, field, objectId, visited))
  }
  return Object.entries(value).some(
    ([key, item]) =>
      (key === field && item === objectId) ||
      hasObjectReference(item, field, objectId, visited),
  )
}

function queryRoot(query: Query): string {
  const root = query.queryKey[0]
  return typeof root === 'string' ? root : ''
}

function queryKeyPart(query: Query, index: number): string | null {
  const value = query.queryKey[index]
  return typeof value === 'string' ? value : null
}

export function shouldInvalidateForEvent(query: Query, event: DurableEvent): boolean {
  const root = queryRoot(query)
  if (root === 'activity') return true

  if (event.object_type === 'material') {
    if (root === 'materials') return true
    if (
      (root === 'material' || root === 'material-memory') &&
      queryKeyPart(query, 1) === event.object_id
    ) {
      return true
    }
    return (
      root === 'project-workspace' &&
      hasObjectReference(query.state.data, 'material_id', event.object_id)
    )
  }

  if (event.object_type === 'project') {
    if (root === 'projects') return true
    return (
      ['project-workspace', 'project-runs', 'project-render-variants'].includes(root) &&
      queryKeyPart(query, 1) === event.object_id
    )
  }

  if (event.object_type === 'run') {
    if (root === 'run' && queryKeyPart(query, 1) === event.object_id) return true
    if (root === 'project-runs') return true
    return (
      ['project-workspace', 'frozen-edit-review', 'project-render-variants'].includes(
        root,
      ) && hasObjectReference(query.state.data, 'run_id', event.object_id)
    )
  }

  if (event.object_type === 'frozen_edit') {
    if (root === 'frozen-edit-review') return true
    if (
      root === 'render-variants' &&
      queryKeyPart(query, 1) === 'edit' &&
      queryKeyPart(query, 2) === event.object_id
    ) {
      return true
    }
    return ['run', 'project-runs', 'project-workspace'].includes(root)
  }

  if (event.object_type === 'render_variant') {
    if (root === 'render-variant' && queryKeyPart(query, 1) === event.object_id) {
      return true
    }
    if (root === 'render-variants' || root === 'project-render-variants') return true
    return (
      ['frozen-edit-review', 'project-workspace'].includes(root) &&
      hasObjectReference(query.state.data, 'render_variant_id', event.object_id)
    )
  }

  if (event.object_type === 'data_root_migration') {
    return (
      root === 'settings-storage-migration-current' ||
      root === 'settings-storage' ||
      root === 'settings' ||
      root === 'health'
    )
  }

  if (
    event.object_type === 'settings' ||
    event.object_type === 'provider_connection' ||
    event.event_type.startsWith('settings.')
  ) {
    return (
      root === 'settings' ||
      root === 'settings-storage' ||
      root === 'settings-storage-migration-current' ||
      root === 'health'
    )
  }

  return false
}

export function invalidateDurableEvent(
  queryClient: QueryClient,
  event: DurableEvent,
): Promise<void> {
  return queryClient.invalidateQueries({
    predicate: (query) => shouldInvalidateForEvent(query, event),
  })
}

export function invalidateAuthoritativeState(queryClient: QueryClient): Promise<void> {
  return queryClient.invalidateQueries({
    predicate: (query) => authoritativeRoots.has(queryRoot(query)),
  })
}

export function parseResyncRequest(value: string): Record<string, unknown> | null {
  try {
    const decoded: unknown = JSON.parse(value)
    return isRecord(decoded) &&
      decoded.schema_version === '1.0' &&
      decoded.action === 'refetch_authoritative_state'
      ? decoded
      : null
  } catch {
    return null
  }
}
