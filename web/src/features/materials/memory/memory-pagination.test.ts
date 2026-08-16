import { describe, expect, it } from 'vitest'

import {
  memoryPagingSummary,
  mergeMemoryPages,
  nextMemoryOffset,
} from '@/features/materials/memory/memory-pagination'
import type { MaterialMemoryResponse } from '@/features/shared/api'

function response(offset: number, items: string[]): MaterialMemoryResponse {
  return {
    material_id: 'mat_video',
    material_type: 'video',
    tab: 'timeline',
    limit: 2,
    offset,
    payload: {
      source: { duration_sec: 10 },
      segments: {
        items: items.map((segment_id) => ({ segment_id })),
        total: 3,
        limit: 2,
        offset,
      },
    },
  }
}

describe('Material Memory pagination', () => {
  it('uses exact offset/limit/total and merges pages without replacing them', () => {
    const first = response(0, ['segment_1', 'segment_2'])
    const second = response(2, ['segment_3'])

    expect(nextMemoryOffset('video', 'timeline', first)).toBe(2)
    expect(nextMemoryOffset('video', 'timeline', second)).toBeUndefined()

    const merged = mergeMemoryPages('video', 'timeline', [first, second])
    expect(
      (merged?.payload.segments as { items: Array<{ segment_id: string }> }).items,
    ).toEqual([
      { segment_id: 'segment_1' },
      { segment_id: 'segment_2' },
      { segment_id: 'segment_3' },
    ])
    expect(merged && memoryPagingSummary('video', 'timeline', merged)).toMatchObject({
      loaded: 3,
      total: 3,
      limit: 2,
      offset: 0,
    })
  })
})
