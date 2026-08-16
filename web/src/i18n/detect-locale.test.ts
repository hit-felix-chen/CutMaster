import { describe, expect, it } from 'vitest'

import { detectLocale, normalizeLocale } from '@/i18n/detect-locale'

describe('locale detection', () => {
  it('normalizes supported language families', () => {
    expect(normalizeLocale('zh-Hans-CN')).toBe('zh-CN')
    expect(normalizeLocale('en-GB')).toBe('en-US')
  })

  it('uses the first supported browser locale', () => {
    expect(detectLocale(['fr-FR', 'zh-TW', 'en-US'])).toBe('zh-CN')
    expect(detectLocale(['fr-FR'])).toBe('en-US')
  })
})
