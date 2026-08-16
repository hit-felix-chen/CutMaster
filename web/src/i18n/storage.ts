import { normalizeLocale, type SupportedLocale } from '@/i18n/detect-locale'

const localeStorageKey = 'cutmaster.locale'

export function readLocalePreference(): SupportedLocale | null {
  try {
    const stored = window.localStorage.getItem(localeStorageKey)
    return stored ? normalizeLocale(stored) : null
  } catch {
    return null
  }
}

export function writeLocalePreference(locale: SupportedLocale) {
  try {
    window.localStorage.setItem(localeStorageKey, locale)
  } catch {
    // Browser storage can be unavailable; the in-memory preference still applies.
  }
}

export function removeLocalePreference() {
  try {
    window.localStorage.removeItem(localeStorageKey)
  } catch {
    // Keep the detected locale when browser storage is unavailable.
  }
}
