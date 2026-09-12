import { WriteButton, WriteForm, WriteInput } from '@/components/ui/WriteControls'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowRight,
  Clapperboard,
  FolderPlus,
  LoaderCircle,
  MoreHorizontal,
  Pencil,
  Search,
  Trash2,
  X,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { appRoutes } from '@/app/routes'
import { ErrorState, LoadingState } from '@/components/ui/AsyncState'
import { OperationProblem } from '@/components/ui/OperationProblem'
import { StatusBadge } from '@/components/ui/StatusBadge'
import {
  ApiError,
  api,
  collectionItems,
  type ProjectSummary,
} from '@/features/shared/api'

export function ProjectsLanding() {
  const { t, i18n } = useTranslation('common')
  const [searchParams, setSearchParams] = useSearchParams()
  const search = searchParams.get('search') ?? ''
  const sort = searchParams.get('sort') ?? 'updated_desc'
  const [createOpen, setCreateOpen] = useState(false)
  const [menuProjectId, setMenuProjectId] = useState<string | null>(null)
  const [renameProject, setRenameProject] = useState<ProjectSummary | null>(null)
  const [deleteProject, setDeleteProject] = useState<ProjectSummary | null>(null)
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
        <WriteButton
          className="button button--primary"
          type="button"
          onClick={() => setCreateOpen(true)}
        >
          <FolderPlus size={16} aria-hidden="true" />
          {t('projects.new')}
        </WriteButton>
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
          {items.map((project) => {
            const videos = project.selected_materials?.video ?? []
            const music = project.selected_materials?.music ?? []
            const menuOpen = menuProjectId === project.project_id
            return (
              <article className="project-card" key={project.project_id}>
                <Link
                  className="project-card__link"
                  to={appRoutes.projectOverview(project.project_id)}
                >
                  <div className="project-card__preview">
                    <Clapperboard
                      className="project-card__preview-fallback"
                      size={38}
                      strokeWidth={1.1}
                      aria-hidden="true"
                    />
                    {project.preview_url ? (
                      <img
                        src={project.preview_url}
                        alt=""
                        loading="lazy"
                        decoding="async"
                        onError={(event) => {
                          event.currentTarget.hidden = true
                        }}
                      />
                    ) : null}
                    <span className="project-card__accent" aria-hidden="true" />
                  </div>
                  <div className="project-card__content">
                    <div className="project-card__heading">
                      <h2>{project.name}</h2>
                      <ArrowRight size={17} aria-hidden="true" />
                    </div>
                    <p>
                      {project.creative_brief?.editing_intent ?? t('projects.noBrief')}
                    </p>
                    <div className="project-card__materials">
                      <span>
                        {t('projects.selectedVideo')}:&nbsp;
                        {videos.length
                          ? videos.map((item) => item.name).join(', ')
                          : t('projects.noVideo')}
                      </span>
                      <span>
                        {t('projects.selectedMusic')}:&nbsp;
                        {music.length
                          ? music.map((item) => item.name).join(', ')
                          : t('projects.noMusic')}
                      </span>
                    </div>
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
                <WriteButton
                  className="icon-button project-card__menu-button"
                  type="button"
                  aria-label={t('projects.projectActions', { name: project.name })}
                  aria-expanded={menuOpen}
                  onClick={() =>
                    setMenuProjectId((current) =>
                      current === project.project_id ? null : project.project_id,
                    )
                  }
                >
                  <MoreHorizontal size={18} aria-hidden="true" />
                </WriteButton>
                {menuOpen ? (
                  <div className="project-card__menu" role="menu">
                    <WriteButton
                      type="button"
                      role="menuitem"
                      onClick={() => {
                        setMenuProjectId(null)
                        setRenameProject(project)
                      }}
                    >
                      <Pencil size={15} aria-hidden="true" />
                      {t('projects.rename')}
                    </WriteButton>
                    <WriteButton
                      className="project-card__menu-danger"
                      type="button"
                      role="menuitem"
                      onClick={() => {
                        setMenuProjectId(null)
                        setDeleteProject(project)
                      }}
                    >
                      <Trash2 size={15} aria-hidden="true" />
                      {t('projects.delete')}
                    </WriteButton>
                  </div>
                ) : null}
              </article>
            )
          })}
        </section>
      ) : null}
      {createOpen ? <CreateProjectDialog onClose={() => setCreateOpen(false)} /> : null}
      {renameProject ? (
        <RenameProjectDialog
          project={renameProject}
          onClose={() => setRenameProject(null)}
        />
      ) : null}
      {deleteProject ? (
        <DeleteProjectDialog
          project={deleteProject}
          onClose={() => setDeleteProject(null)}
        />
      ) : null}
    </div>
  )
}

function RenameProjectDialog({
  project,
  onClose,
}: {
  project: ProjectSummary
  onClose: () => void
}) {
  const { t } = useTranslation('common')
  const queryClient = useQueryClient()
  const [name, setName] = useState(project.name)
  const rename = useMutation({
    mutationFn: () => api.projects.rename(project.project_id, name.trim()),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['projects'] }),
        queryClient.invalidateQueries({
          queryKey: ['project-workspace', project.project_id],
        }),
      ])
      onClose()
    },
  })
  return (
    <div className="dialog-layer" role="presentation">
      <WriteForm
        className="dialog"
        role="dialog"
        aria-labelledby="rename-project-title"
        onSubmit={(event) => {
          event.preventDefault()
          if (name.trim() && name.trim() !== project.name) rename.mutate()
        }}
      >
        <header>
          <h2 id="rename-project-title">{t('projects.renameTitle')}</h2>
          <button
            className="icon-button"
            type="button"
            onClick={onClose}
            aria-label={t('common.close')}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>
        <label className="field">
          <span>{t('projects.projectName')}</span>
          <WriteInput
            required
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        {rename.isError ? <OperationProblem error={rename.error} /> : null}
        <footer>
          <button className="button button--secondary" type="button" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <WriteButton
            className="button button--primary"
            disabled={!name.trim() || name.trim() === project.name || rename.isPending}
            type="submit"
          >
            {rename.isPending ? (
              <LoaderCircle className="spin" size={15} aria-hidden="true" />
            ) : null}
            {t('projects.rename')}
          </WriteButton>
        </footer>
      </WriteForm>
    </div>
  )
}

function DeleteProjectDialog({
  project,
  onClose,
}: {
  project: ProjectSummary
  onClose: () => void
}) {
  const { t } = useTranslation('common')
  const queryClient = useQueryClient()
  const remove = useMutation({
    mutationFn: () => api.projects.delete(project.project_id),
    onSuccess: async () => {
      queryClient.removeQueries({
        queryKey: ['project-workspace', project.project_id],
      })
      queryClient.removeQueries({ queryKey: ['project-runs', project.project_id] })
      await queryClient.invalidateQueries({ queryKey: ['projects'] })
      onClose()
    },
  })
  return (
    <div className="dialog-layer" role="presentation">
      <div className="dialog" role="alertdialog" aria-labelledby="delete-project-title">
        <header>
          <h2 id="delete-project-title">{t('projects.deleteTitle')}</h2>
          <button
            className="icon-button"
            type="button"
            disabled={remove.isPending}
            onClick={onClose}
            aria-label={t('common.close')}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>
        <p>{t('projects.deleteBody', { name: project.name })}</p>
        {remove.isError ? <OperationProblem error={remove.error} /> : null}
        <footer>
          <button
            className="button button--secondary"
            type="button"
            disabled={remove.isPending}
            onClick={onClose}
          >
            {t('common.cancel')}
          </button>
          <WriteButton
            className="button button--danger"
            type="button"
            disabled={remove.isPending}
            onClick={() => remove.mutate()}
          >
            {remove.isPending ? (
              <LoaderCircle className="spin" size={15} aria-hidden="true" />
            ) : (
              <Trash2 size={15} aria-hidden="true" />
            )}
            {t('projects.confirmDelete')}
          </WriteButton>
        </footer>
      </div>
    </div>
  )
}

function CreateProjectDialog({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation('common')
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const create = useMutation({
    mutationFn: () => api.projects.create(name),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['projects'] })
      onClose()
    },
  })
  const conflict = projectNameConflict(create.error)
  if (conflict) {
    return (
      <div className="dialog-layer" role="presentation">
        <div
          className="dialog"
          role="alertdialog"
          aria-labelledby="create-project-conflict-title"
          aria-describedby="create-project-conflict-body"
        >
          <header>
            <h2 id="create-project-conflict-title">
              {t('projects.createConflictTitle')}
            </h2>
            <button
              className="icon-button"
              type="button"
              onClick={onClose}
              aria-label={t('common.close')}
            >
              <X size={18} aria-hidden="true" />
            </button>
          </header>
          <p id="create-project-conflict-body">
            {t('projects.createConflictBody', { name: conflict.projectName })}
          </p>
          <footer>
            <button
              className="button button--secondary"
              type="button"
              onClick={() => create.reset()}
            >
              {t('projects.createConflictBack')}
            </button>
            <button
              className="button button--primary"
              type="button"
              onClick={() => navigate(appRoutes.projectOverview(conflict.projectId))}
            >
              {t('projects.createConflictOpen')}
            </button>
          </footer>
        </div>
      </div>
    )
  }
  return (
    <div
      className="dialog-layer"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose()
      }}
    >
      <WriteForm
        className="dialog"
        role="dialog"
        aria-labelledby="create-project-title"
        onSubmit={(event) => {
          event.preventDefault()
          create.mutate()
        }}
      >
        <header>
          <h2 id="create-project-title">{t('projects.createTitle')}</h2>
          <button
            className="icon-button"
            type="button"
            onClick={onClose}
            aria-label={t('common.close')}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>
        <label className="field">
          <span>{t('projects.projectName')}</span>
          <WriteInput
            required
            value={name}
            onChange={(event) => setName(event.target.value)}
          />
        </label>
        {create.isError ? <OperationProblem error={create.error} /> : null}
        <footer>
          <button className="button button--secondary" type="button" onClick={onClose}>
            {t('common.cancel')}
          </button>
          <WriteButton
            className="button button--primary"
            disabled={!name.trim() || create.isPending}
            type="submit"
          >
            {t(create.isPending ? 'projects.creating' : 'projects.create')}
          </WriteButton>
        </footer>
      </WriteForm>
    </div>
  )
}

function projectNameConflict(error: unknown) {
  if (
    !(error instanceof ApiError) ||
    error.status !== 409 ||
    error.problem?.code !== 'project_name_conflict'
  ) {
    return null
  }
  const projectId = error.problem.parameters?.project_id
  const projectName = error.problem.parameters?.project_name
  if (typeof projectId !== 'string' || typeof projectName !== 'string') {
    return null
  }
  return { projectId, projectName }
}
