export const supportedLocales = ['zh-CN', 'en-US'] as const
export type SupportedLocale = (typeof supportedLocales)[number]

export function normalizeLocale(value: string): SupportedLocale | null {
  const normalized = value.trim().toLowerCase()
  if (normalized === 'zh-cn' || normalized.startsWith('zh')) return 'zh-CN'
  if (normalized === 'en-us' || normalized.startsWith('en')) return 'en-US'
  return null
}

export function detectLocale(
  languages: readonly string[] = navigator.languages,
): SupportedLocale {
  for (const language of languages) {
    const locale = normalizeLocale(language)
    if (locale) return locale
  }
  return 'en-US'
}
