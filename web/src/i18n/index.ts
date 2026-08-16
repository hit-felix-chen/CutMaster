import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'

import { detectLocale, type SupportedLocale } from '@/i18n/detect-locale'
import enCommon from '@/i18n/locales/en-US/common.json'
import zhCommon from '@/i18n/locales/zh-CN/common.json'
import { readLocalePreference } from '@/i18n/storage'

void i18n.use(initReactI18next).init({
  resources: {
    'zh-CN': { common: zhCommon },
    'en-US': { common: enCommon },
  },
  lng: readLocalePreference() ?? detectLocale(),
  fallbackLng: 'en-US',
  supportedLngs: ['zh-CN', 'en-US'] satisfies SupportedLocale[],
  defaultNS: 'common',
  interpolation: { escapeValue: false },
  returnNull: false,
})

export { detectLocale }
export type { SupportedLocale }
export default i18n
