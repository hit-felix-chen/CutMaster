import { useMutation, useQuery } from '@tanstack/react-query'
import {
  Database,
  FolderOpen,
  HardDrive,
  Languages,
  LoaderCircle,
  Moon,
} from 'lucide-react'
import { useTranslation } from 'react-i18next'

import { useColorMode } from '@/app/providers/ColorModeProvider'
import { useLocale } from '@/app/providers/LocaleProvider'
import { useApplicationHealth } from '@/app/use-application-health'
import { ErrorState, LoadingState } from '@/components/ui/AsyncState'
import { OperationProblem } from '@/components/ui/OperationProblem'
import { api } from '@/features/shared/api'
import { DataRootMigrationPanel } from '@/features/settings/DataRootMigrationPanel'
import { ProviderSettingsEditor } from '@/features/settings/ProviderSettingsEditor'
import {
  isDataRootMigrationBlocking,
  useCurrentDataRootMigration,
} from '@/features/settings/data-root-migration'
import { formatBytes } from '@/i18n/formatters'

export function SettingsWorkspace() {
  const { t, i18n } = useTranslation('common')
  const health = useApplicationHealth()
  const migration = useCurrentDataRootMigration()
  const rootBlocked = Boolean(
    health.data?.data_root?.maintenance ||
    health.data?.data_root?.restart_required ||
    isDataRootMigrationBlocking(migration.data?.migration),
  )
  const settings = useQuery({
    queryKey: ['settings'],
    queryFn: api.settings.get,
    enabled: !rootBlocked,
  })
  const storage = useQuery({
    queryKey: ['settings-storage'],
    queryFn: api.settings.storage,
    enabled: !rootBlocked,
  })
  const revealStorage = useMutation({ mutationFn: api.settings.revealStorage })
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
        {settings.isPending && !rootBlocked ? (
          <section className="settings-section">
            <LoadingState />
          </section>
        ) : null}
        {settings.isError && !rootBlocked ? (
          <section className="settings-section">
            <ErrorState onRetry={() => void settings.refetch()} />
          </section>
        ) : null}
        {settings.data && !rootBlocked ? (
          <ProviderSettingsEditor settings={settings.data} />
        ) : null}
        <section className="settings-section settings-section--storage">
          <header>
            <HardDrive size={18} />
            <div>
              <h2>{t('settings.storage')}</h2>
            </div>
          </header>
          {storage.isPending && !rootBlocked ? <LoadingState /> : null}
          {storage.isError && !rootBlocked ? (
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
                {storage.data.reveal_supported ? (
                  <button
                    className="button button--secondary"
                    type="button"
                    disabled={revealStorage.isPending || rootBlocked}
                    onClick={() => revealStorage.mutate()}
                  >
                    {revealStorage.isPending ? (
                      <LoaderCircle className="spin" size={15} />
                    ) : (
                      <FolderOpen size={15} />
                    )}
                    {t('settings.openInFinder')}
                  </button>
                ) : null}
              </div>
              {revealStorage.isSuccess ? (
                <p className="settings-storage-status" role="status">
                  {t('settings.openedInFinder')}
                </p>
              ) : null}
              {revealStorage.isError ? (
                <OperationProblem error={revealStorage.error} />
              ) : null}
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
            </>
          ) : null}
          <DataRootMigrationPanel
            applicationBlocked={Boolean(
              health.data?.data_root?.maintenance ||
              health.data?.data_root?.restart_required,
            )}
          />
        </section>
      </div>
    </div>
  )
}
