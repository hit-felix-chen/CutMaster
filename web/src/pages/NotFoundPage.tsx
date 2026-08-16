import { ArrowLeft, MapPinOff } from 'lucide-react'
import { useTranslation } from 'react-i18next'
import { Link } from 'react-router-dom'

import { appPaths } from '@/app/routes'

export function NotFoundPage() {
  const { t } = useTranslation('common')
  return (
    <div className="page">
      <section className="empty-panel">
        <MapPinOff size={30} />
        <h1>{t('errors.notFound')}</h1>
        <p>{t('errors.notFoundBody')}</p>
        <Link className="button button--secondary" to={appPaths.projects}>
          <ArrowLeft size={16} />
          {t('errors.backProjects')}
        </Link>
      </section>
    </div>
  )
}
