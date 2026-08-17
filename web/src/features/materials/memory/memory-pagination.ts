import type { MaterialMemoryResponse, MaterialType } from '@/features/shared/api'

type DataRecord = Record<string, unknown>

export const MEMORY_PAGE_SIZE = 100
export const MUSIC_MEMORY_PAGE_SIZE = 500

function record(value: unknown): DataRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as DataRecord)
    : {}
}

function page(value: unknown) {
  const collection = record(value)
  return {
    items: Array.isArray(collection.items) ? collection.items : [],
    total:
      typeof collection.total === 'number' && Number.isFinite(collection.total)
        ? collection.total
        : 0,
    limit:
      typeof collection.limit === 'number' && Number.isFinite(collection.limit)
        ? collection.limit
        : MEMORY_PAGE_SIZE,
    offset:
      typeof collection.offset === 'number' && Number.isFinite(collection.offset)
        ? collection.offset
        : 0,
  }
}

function collectionKeys(type: MaterialType, tab: string) {
  if (type === 'video' && tab === 'timeline') return ['segments']
  if (type === 'video' && tab === 'dialogue') return ['sentences']
  if (type === 'music' && tab === 'structure') {
    return ['beats_sec', 'accents_sec', 'energy_curve', 'sections']
  }
  return []
}

export interface MemoryPagingSummary {
  loaded: number
  total: number
  limit: number
  offset: number
}

export function memoryPagingSummary(
  type: MaterialType,
  tab: string,
  response: MaterialMemoryResponse,
): MemoryPagingSummary | null {
  const keys = collectionKeys(type, tab)
  if (!keys.length) return null
  const pages = keys.map((key) => page(response.payload[key]))
  return {
    loaded: Math.max(...pages.map((item) => item.items.length)),
    total: Math.max(...pages.map((item) => item.total)),
    limit: response.limit ?? pages[0]?.limit ?? MEMORY_PAGE_SIZE,
    offset: response.offset ?? pages[0]?.offset ?? 0,
  }
}

export function nextMemoryOffset(
  type: MaterialType,
  tab: string,
  response: MaterialMemoryResponse,
) {
  const summary = memoryPagingSummary(type, tab, response)
  if (!summary) return undefined
  const next = summary.offset + summary.limit
  return next < summary.total ? next : undefined
}

export function mergeMemoryPages(
  type: MaterialType,
  tab: string,
  responses: MaterialMemoryResponse[],
): MaterialMemoryResponse | null {
  const first = responses[0]
  if (!first) return null
  const keys = collectionKeys(type, tab)
  if (!keys.length) return first

  const payload: DataRecord = { ...first.payload }
  for (const key of keys) {
    const collections = responses.map((response) => page(response.payload[key]))
    const items = collections.flatMap((collection) => collection.items)
    payload[key] = {
      items,
      total: Math.max(...collections.map((collection) => collection.total)),
      limit: first.limit,
      offset: 0,
    }
  }
  return {
    ...first,
    limit: first.limit,
    offset: 0,
    payload,
  }
}
