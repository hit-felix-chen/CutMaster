import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, Clapperboard, FolderPlus, Search, X } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useSearchParams } from 'react-router-dom'

import { appRoutes } from '@/app/routes'
import { ErrorState, LoadingState } from '@/components/ui/AsyncState'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { api, collectionItems } from '@/features/shared/api'

export function ProjectsLanding() {
  const { t, i18n } = useTranslation('common')
  const [searchParams, setSearchParams] = useSearchParams()
  const search = searchParams.get('search') ?? ''
  const sort = searchParams.get('sort') ?? 'updated_desc'
  const [createOpen, setCreateOpen] = useState(false)
  const projects = useQuery({
    queryKey: ['projects', search, sort],
    queryFn: () => api.projects.list(),
  })
  const items = useMemo(() => {
    const values = projects.data ? collectionItems(projects.data) : []
    const needle = search.trim().toLocaleLowerCase()
    const filtered = needle
      ? values.filter((project) => project.name.toLocaleLowerCase().includes(needle))
      : values
    return [...filtered].sort((left, right) => {
      if (sort.startsWith('name')) {
        const result = left.name.localeCompare(right.name)
        return sort === 'name_desc' ? -result : result
      }
      const result = left.updated_at.localeCompare(right.updated_at)
      return sort === 'updated_asc' ? result : -result
    })
  }, [projects.data, search, sort])

  const updateFilter = (key: string, value: string) => {
    const next = new URLSearchParams(searchParams)
    if (value) next.set(key, value)
    else next.delete(key)
    setSearchParams(next, { replace: true })
  }

  return (
    <div className="page">
      <header className="page-header">
        <div>
          <span className="eyebrow">The MASTER Editing Team</span>
          <h1>{t('projects.title')}</h1>
          <p>{t('projects.subtitle')}</p>
        </div>
        <button
          className="button button--primary"
          type="button"
          onClick={() => setCreateOpen(true)}
        >
          <FolderPlus size={16} aria-hidden="true" />
          {t('projects.new')}
        </button>
      </header>
      <div className="toolbar">
        <label className="search-control">
          <Search size={16} aria-hidden="true" />
          <input
            value={search}
            onChange={(event) => updateFilter('search', event.target.value)}
            placeholder={t('common.search')}
          />
        </label>
        <label className="sort-control">
          <span className="sr-only">{t('common.status')}</span>
          <select
            value={sort}
            onChange={(event) => updateFilter('sort', event.target.value)}
          >
            <option value="updated_desc">{t('common.updated')} ↓</option>
            <option value="updated_asc">{t('common.updated')} ↑</option>
            <option value="name_asc">{t('common.name')} ↑</option>
            <option value="name_desc">{t('common.name')} ↓</option>
          </select>
        </label>
      </div>
      {projects.isPending ? <LoadingState /> : null}
      {projects.isError ? <ErrorState onRetry={() => void projects.refetch()} /> : null}
      {projects.isSuccess && items.length === 0 ? (
        <section className="empty-panel">
          <Clapperboard size={30} aria-hidden="true" />
          <h2>{t('projects.emptyTitle')}</h2>
          <p>{t('projects.emptyBody')}</p>
        </section>
      ) : null}
      {items.length ? (
        <section className="project-grid">
          {items.map((project) => (
            <Link
              className="project-card"
              key={project.project_id}
              to={appRoutes.projectOverview(project.project_id)}
            >
              <div className="project-card__preview">
                {project.preview_url ? (
                  <img src={project.preview_url} alt="" />
                ) : (
                  <Clapperboard size={38} strokeWidth={1.1} aria-hidden="true" />
                )}
                <span className="project-card__accent" aria-hidden="true" />
              </div>
              <div className="project-card__content">
                <div className="project-card__heading">
                  <h2>{project.name}</h2>
                  <ArrowRight size={17} aria-hidden="true" />
                </div>
                <p>{project.creative_brief?.editing_intent ?? t('projects.noBrief')}</p>
                <footer>
                  {project.latest_run_state ? (
                    <StatusBadge status={project.latest_run_state} />
                  ) : (
                    <span>{t('projects.noRuns')}</span>
                  )}
                  <time dateTime={project.updated_at}>
                    {new Intl.DateTimeFormat(i18n.language, {
                      dateStyle: 'medium',
                    }).format(new Date(project.updated_at))}
                  </time>
                </footer>
              </div>
            </Link>
          ))}
        </section>
      ) : null}
      {createOpen ? <CreateProjectDialog onClose={() => setCreateOpen(false)} /> : null}
    </div>
  )
}

function CreateProjectDialog({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation('common')
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const create = useMutation({
    mutationFn: () => api.projects.create(name),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['projects'] })
      onClose()
    },
  })
  return (
    <div
      className="dialog-layer"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose()
      }}
    >
      <form
        className="dialog"
        onSubmit={(event) => {
          event.preventDefault()
          create.mutate()
        }}
      >
        <header>
          <h2>{t('projects.createTitle')}</h2>
          <button
            className="icon-button"
            type="button"
            onClick={onClose}
            aria-label={t('common.close')}
          >
            <X size={18} />
          </button>
        </header>
        <label className="field">
          <span>{t('projects.projectName')}</span>
          <input
            required
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        {create.isError ? <ErrorState /> : null}
        <footer>
          <button className="button button--secondary" type="button" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <button
            className="button button--primary"
            disabled={!name.trim() || create.isPending}
            type="submit"
          >
            {t(create.isPending ? 'projects.creating' : 'projects.create')}
          </button>
        </footer>
      </form>
    </div>
  )
}
