import type { Location } from 'react-router-dom'

export type RevisionDraft = Record<string, string>

export interface RevisionReplacement {
  group_id: string
  trajectory_id: string
}

export function revisionReplacements(draft: RevisionDraft): RevisionReplacement[] {
  return Object.entries(draft)
    .map(([group_id, trajectory_id]) => ({ group_id, trajectory_id }))
    .sort((left, right) => left.group_id.localeCompare(right.group_id))
}

export function changesReviewContext(current: Location, next: Location): boolean {
  if (current.pathname !== next.pathname) return true
  const currentVariant = new URLSearchParams(current.search).get('variant')
  const nextVariant = new URLSearchParams(next.search).get('variant')
  return currentVariant !== nextVariant
}
