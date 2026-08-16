import type { Location } from 'react-router-dom'

export type RevisionDraft = Record<string, string>

export interface RevisionReplacement {
  slot_id: string
  candidate_id: string
}

export function revisionReplacements(draft: RevisionDraft): RevisionReplacement[] {
  return Object.entries(draft)
    .map(([slot_id, candidate_id]) => ({ slot_id, candidate_id }))
    .sort((left, right) => left.slot_id.localeCompare(right.slot_id))
}

export function changesReviewContext(current: Location, next: Location): boolean {
  if (current.pathname !== next.pathname) return true
  const currentVariant = new URLSearchParams(current.search).get('variant')
  const nextVariant = new URLSearchParams(next.search).get('variant')
  return currentVariant !== nextVariant
}
