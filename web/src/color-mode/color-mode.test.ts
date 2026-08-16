import { describe, expect, it } from 'vitest'

import { resolveColorMode } from '@/color-mode/color-mode'

describe('colour mode resolution', () => {
  it('keeps explicit modes', () => {
    expect(resolveColorMode('dark', false)).toBe('dark')
    expect(resolveColorMode('light', true)).toBe('light')
  })

  it('resolves system mode from the media preference', () => {
    expect(resolveColorMode('system', true)).toBe('dark')
    expect(resolveColorMode('system', false)).toBe('light')
  })
})
