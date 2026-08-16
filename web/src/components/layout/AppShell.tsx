import { Activity, DatabaseBackup, FolderKanban, Library, Settings } from 'lucide-react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { EventStreamStatus } from '@/app/providers/EventStreamProvider'
import { appPaths } from '@/app/routes'
import { useApplicationHealth } from '@/app/use-application-health'
import {
  isDataRootMigrationBlocking,
  useCurrentDataRootMigration,
} from '@/features/settings/data-root-migration'

const navigation = [
  { to: appPaths.projects, key: 'nav.projects', icon: FolderKanban },
  { to: appPaths.materials, key: 'nav.materials', icon: Library },
  { to: appPaths.activity, key: 'nav.activity', icon: Activity },
] as const

export function AppShell() {
  const { t } = useTranslation('common')
  const location = useLocation()
  const health = useApplicationHealth()
  const migration = useCurrentDataRootMigration()
  const currentMigration = migration.data?.migration
  const restartRequired = Boolean(
    health.data?.data_root?.restart_required ||
    currentMigration?.status === 'restart_required',
  )
  const rootBlocked = Boolean(
    restartRequired ||
    health.data?.data_root?.maintenance ||
    isDataRootMigrationBlocking(currentMigration),
  )
  const settingsVisible = location.pathname.startsWith(appPaths.settings)
  return (
    <>
      <div className="viewport-guard">
        <div className="viewport-guard__mark" aria-hidden="true">
          M
        </div>
        <h1>{t('shell.narrowTitle')}</h1>
        <p>{t('shell.narrowBody')}</p>
      </div>
      <div className="app-shell">
        <aside className="app-rail">
          {rootBlocked ? (
            <span className="rail-brand" aria-label={t('appName')}>
              <span className="rail-brand__mark" aria-hidden="true">
                M
              </span>
              <span>CutMaster</span>
            </span>
          ) : (
            <NavLink
              className="rail-brand"
              to={appPaths.projects}
              aria-label={t('appName')}
            >
              <span className="rail-brand__mark" aria-hidden="true">
                M
              </span>
              <span>CutMaster</span>
            </NavLink>
          )}
          <nav className="rail-navigation" aria-label={t('appName')}>
            {navigation.map(({ to, key, icon: Icon }) =>
              rootBlocked ? (
                <span
                  key={to}
                  className="rail-link rail-link--disabled"
                  aria-disabled="true"
                >
                  <Icon size={19} aria-hidden="true" />
                  <span>{t(key)}</span>
                </span>
              ) : (
                <NavLink key={to} className="rail-link" to={to}>
                  <Icon size={19} aria-hidden="true" />
                  <span>{t(key)}</span>
                </NavLink>
              ),
            )}
          </nav>
          <NavLink className="rail-link rail-link--settings" to={appPaths.settings}>
            <Settings size={19} aria-hidden="true" />
            <span>{t('nav.settings')}</span>
          </NavLink>
          <EventStreamStatus />
        </aside>
        <main id="main-content" className="app-main">
          {rootBlocked ? (
            <section
              className={`data-root-global-banner data-root-global-banner--${restartRequired ? 'restart' : 'maintenance'}`}
              role="alert"
              aria-live="assertive"
            >
              <DatabaseBackup size={20} aria-hidden="true" />
              <div>
                <strong>
                  {t(
                    restartRequired
                      ? 'settings.dataRootMigration.globalRestartTitle'
                      : 'settings.dataRootMigration.globalMaintenanceTitle',
                  )}
                </strong>
                <p>
                  {t(
                    restartRequired
                      ? 'settings.dataRootMigration.globalRestartBody'
                      : 'settings.dataRootMigration.globalMaintenanceBody',
                  )}
                </p>
              </div>
              {!settingsVisible ? (
                <Link className="button button--secondary" to={appPaths.settings}>
                  {t('settings.dataRootMigration.manage')}
                </Link>
              ) : null}
            </section>
          ) : null}
          {rootBlocked && !settingsVisible ? (
            <section className="data-root-workspace-blocker">
              <DatabaseBackup size={28} aria-hidden="true" />
              <h1>
                {t(
                  restartRequired
                    ? 'settings.dataRootMigration.restartTitle'
                    : 'settings.dataRootMigration.migrationInProgress',
                )}
              </h1>
              <p>
                {t(
                  restartRequired
                    ? 'settings.dataRootMigration.restartBody'
                    : 'settings.dataRootMigration.actionsPaused',
                )}
              </p>
              <Link className="button button--primary" to={appPaths.settings}>
                {t('settings.dataRootMigration.manage')}
              </Link>
            </section>
          ) : (
            <Outlet />
          )}
        </main>
      </div>
    </>
  )
}
