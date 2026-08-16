import { describe, expect, it } from 'vitest'

import { createAppQueryClient } from '@/app/query-client'
import { ApiError } from '@/features/shared/api'

describe('application query retries', () => {
  it('does not retry stable client errors and still retries transient failures', () => {
    const retry = createAppQueryClient().getDefaultOptions().queries?.retry
    expect(typeof retry).toBe('function')
    if (typeof retry !== 'function') return

    expect(retry(0, new ApiError(409, null))).toBe(false)
    expect(retry(0, new Error('temporary connection failure'))).toBe(true)
    expect(retry(2, new Error('temporary connection failure'))).toBe(false)
  })
})
