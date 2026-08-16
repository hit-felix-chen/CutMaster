import { useQuery } from '@tanstack/react-query'
import {
  CheckCircle2,
  CircleAlert,
  Database,
  HardDrive,
  Languages,
  Moon,
  ServerCog,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { useColorMode } from '@/app/providers/ColorModeProvider'
import { useLocale } from '@/app/providers/LocaleProvider'
import { ErrorState, LoadingState } from '@/components/ui/AsyncState'
import { api } from '@/features/shared/api'
import { formatBytes } from '@/i18n/formatters'

export function SettingsWorkspace() {
  const { t, i18n } = useTranslation('common')
  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings.get })
  const storage = useQuery({
    queryKey: ['settings-storage'],
    queryFn: api.settings.storage,
  })
  const { locale, setLocale } = useLocale()
  const { colorMode, setColorMode } = useColorMode()
  return (
    <div className="page">
      <header className="page-header">
        <div>
          <span className="eyebrow">{t('settings.localApplication')}</span>
          <h1>{t('settings.title')}</h1>
          <p>{t('settings.subtitle')}</p>
        </div>
      </header>
      <div className="settings-layout">
        <section className="settings-section">
          <header>
            <Moon size={18} />
            <div>
              <h2>{t('settings.appearance')}</h2>
            </div>
          </header>
          <div className="settings-control-row">
            <span>
              <Languages size={16} />
              {t('settings.language')}
            </span>
            <div className="segmented-control">
              <button
                type="button"
                className={locale === 'zh-CN' ? 'active' : ''}
                onClick={() => setLocale('zh-CN')}
              >
                简体中文
              </button>
              <button
                type="button"
                className={locale === 'en-US' ? 'active' : ''}
                onClick={() => setLocale('en-US')}
              >
                English
              </button>
            </div>
          </div>
          <div className="settings-control-row">
            <span>
              <Moon size={16} />
              {t('settings.colourMode')}
            </span>
            <div className="segmented-control">
              {(['dark', 'light', 'system'] as const).map((mode) => (
                <button
                  key={mode}
                  type="button"
                  className={colorMode === mode ? 'active' : ''}
                  onClick={() => setColorMode(mode)}
                >
                  {t(mode)}
                </button>
              ))}
            </div>
          </div>
        </section>
        <section className="settings-section">
          <header>
            <ServerCog size={18} />
            <div>
              <h2>{t('settings.connections')}</h2>
              <p>{t('settings.readOnly')}</p>
            </div>
          </header>
          {settings.isPending ? <LoadingState /> : null}
          {settings.isError ? (
            <ErrorState onRetry={() => void settings.refetch()} />
          ) : null}
          {settings.data ? (
            <div className="connection-grid">
              {(['llm', 'vlm', 'asr'] as const).map((provider) => {
                const configured = settings.data.secrets[`${provider}_configured`]
                return (
                  <article key={provider}>
                    <span>{t(`settings.${provider}`)}</span>
                    {configured ? (
                      <CheckCircle2 className="success" />
                    ) : (
                      <CircleAlert className="warning" />
                    )}
                    <strong>
                      {t(configured ? 'settings.configured' : 'settings.missing')}
                    </strong>
                  </article>
                )
              })}
            </div>
          ) : null}
        </section>
        <section className="settings-section settings-section--storage">
          <header>
            <HardDrive size={18} />
            <div>
              <h2>{t('settings.storage')}</h2>
            </div>
          </header>
          {storage.isPending ? <LoadingState /> : null}
          {storage.isError ? (
            <ErrorState onRetry={() => void storage.refetch()} />
          ) : null}
          {storage.data ? (
            <>
              <div className="data-root">
                <Database size={16} />
                <span>
                  <small>{t('settings.dataRoot')}</small>
                  <code>{storage.data.data_root}</code>
                </span>
                <strong>
                  {formatBytes(storage.data.total_size_bytes, i18n.language)}
                </strong>
              </div>
              <div className="storage-grid">
                {storage.data.categories.map((category) => (
                  <article key={category.name}>
                    <span>
                      {t(`settings.storageCategories.${category.name}`, {
                        defaultValue: category.name,
                      })}
                    </span>
                    <strong>{formatBytes(category.size_bytes, i18n.language)}</strong>
                    <small>{category.file_count}</small>
                  </article>
                ))}
              </div>
              <p>
                {t('settings.directBundles')}: {storage.data.direct_bundle_count}
              </p>
            </>
          ) : null}
        </section>
      </div>
    </div>
  )
}
