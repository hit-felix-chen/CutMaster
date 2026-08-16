import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type PropsWithChildren,
} from 'react'

import i18n, { detectLocale, type SupportedLocale } from '@/i18n'
import {
  readLocalePreference,
  removeLocalePreference,
  writeLocalePreference,
} from '@/i18n/storage'

type LocaleContextValue = {
  locale: SupportedLocale
  explicitLocale: SupportedLocale | null
  setLocale: (locale: SupportedLocale) => void
  resetLocale: () => void
}

const LocaleContext = createContext<LocaleContextValue | null>(null)

export function LocaleProvider({ children }: PropsWithChildren) {
  const [explicitLocale, setExplicitLocale] = useState(readLocalePreference)
  const locale = explicitLocale ?? detectLocale()

  useEffect(() => {
    document.documentElement.lang = locale
    void i18n.changeLanguage(locale)
  }, [locale])

  const setLocale = useCallback((nextLocale: SupportedLocale) => {
    writeLocalePreference(nextLocale)
    setExplicitLocale(nextLocale)
  }, [])

  const resetLocale = useCallback(() => {
    removeLocalePreference()
    setExplicitLocale(null)
  }, [])

  const value = useMemo(
    () => ({ locale, explicitLocale, setLocale, resetLocale }),
    [explicitLocale, locale, resetLocale, setLocale],
  )

  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>
}

export function useLocale(): LocaleContextValue {
  const context = useContext(LocaleContext)
  if (!context) {
    throw new Error('useLocale must be used within LocaleProvider.')
  }
  return context
}
