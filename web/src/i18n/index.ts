import i18n from 'i18next'
import { initReactI18next } from 'react-i18next'

import { detectLocale, type SupportedLocale } from '@/i18n/detect-locale'
import enCommon from '@/i18n/locales/en-US/common.json'
import enProblems from '@/i18n/locales/en-US/problems.json'
import zhCommon from '@/i18n/locales/zh-CN/common.json'
import zhProblems from '@/i18n/locales/zh-CN/problems.json'
import { readLocalePreference } from '@/i18n/storage'

void i18n.use(initReactI18next).init({
  resources: {
    'zh-CN': { common: zhCommon, problems: zhProblems },
    'en-US': { common: enCommon, problems: enProblems },
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
