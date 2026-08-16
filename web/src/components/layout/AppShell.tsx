import { Activity, FolderKanban, Library, Settings } from 'lucide-react'
import { NavLink, Outlet } from 'react-router-dom'
import { useTranslation } from 'react-i18next'

import { appPaths } from '@/app/routes'

const navigation = [
  { to: appPaths.projects, key: 'nav.projects', icon: FolderKanban },
  { to: appPaths.materials, key: 'nav.materials', icon: Library },
  { to: appPaths.activity, key: 'nav.activity', icon: Activity },
] as const

export function AppShell() {
  const { t } = useTranslation('common')
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
          <nav className="rail-navigation" aria-label={t('appName')}>
            {navigation.map(({ to, key, icon: Icon }) => (
              <NavLink key={to} className="rail-link" to={to}>
                <Icon size={19} aria-hidden="true" />
                <span>{t(key)}</span>
              </NavLink>
            ))}
          </nav>
          <NavLink className="rail-link rail-link--settings" to={appPaths.settings}>
            <Settings size={19} aria-hidden="true" />
            <span>{t('nav.settings')}</span>
          </NavLink>
        </aside>
        <main id="main-content" className="app-main">
          <Outlet />
        </main>
      </div>
    </>
  )
}
