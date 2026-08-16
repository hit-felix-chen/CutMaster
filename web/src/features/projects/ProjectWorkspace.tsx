import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AudioWaveform,
  ChevronLeft,
  Film,
  Layers3,
  PlaySquare,
  Rocket,
  Save,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import {
  Link,
  NavLink,
  Outlet,
  useBlocker,
  useNavigate,
  useOutletContext,
  useParams,
} from 'react-router-dom'

import { appPaths, appRoutes } from '@/app/routes'
import { ErrorState, LoadingState } from '@/components/ui/AsyncState'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { AsterProgress } from '@/features/shared/AsterProgress'
import {
  api,
  collectionItems,
  type MaterialSummary,
  type ProjectWorkspace as WorkspaceData,
} from '@/features/shared/api'
import {
  executionStatus,
  hasActiveRun,
  isRunExecutionActive,
} from '@/features/shared/execution-state'

interface ProjectContext {
  workspace: WorkspaceData
  refetch: () => Promise<unknown>
}

export function ProjectLayout() {
  const { t } = useTranslation('common')
  const { projectId = '' } = useParams()
  const workspace = useQuery({
    queryKey: ['project-workspace', projectId],
    queryFn: () => api.projects.workspace(projectId),
    enabled: Boolean(projectId),
  })
  if (workspace.isPending)
    return (
      <div className="page">
        <LoadingState />
      </div>
    )
  if (workspace.isError)
    return (
      <div className="page">
        <ErrorState onRetry={() => void workspace.refetch()} />
      </div>
    )
  const tabs = [
    [appRoutes.projectOverview(projectId), 'projects.setup'],
    [appRoutes.projectRuns(projectId), 'projects.runs'],
    [appRoutes.projectOutputs(projectId), 'projects.outputs'],
  ] as const
  return (
    <div className="project-workspace">
      <header className="project-header">
        <Link className="project-header__back" to={appPaths.projects}>
          <ChevronLeft size={16} />
          {t('nav.projects')}
        </Link>
        <div className="project-header__title">
          <span className="eyebrow">{t('projects.editProject')}</span>
          <h1>{workspace.data.project.name}</h1>
        </div>
        <nav className="project-tabs">
          {tabs.map(([to, key]) => (
            <NavLink key={to} to={to}>
              {t(key)}
            </NavLink>
          ))}
        </nav>
      </header>
      <div className="project-content">
        <Outlet
          context={
            {
              workspace: workspace.data,
              refetch: workspace.refetch,
            } satisfies ProjectContext
          }
        />
      </div>
    </div>
  )
}

function useProjectContext() {
  return useOutletContext<ProjectContext>()
}

function MaterialPanel({
  type,
  material,
  editable = false,
}: {
  type: 'video' | 'music'
  material?: MaterialSummary
  editable?: boolean
}) {
  const { t } = useTranslation('common')
  const Icon = type === 'video' ? Film : AudioWaveform
  return (
    <article className="summary-panel material-selection-panel">
      <header>
        <span>
          <Icon size={16} />
          {t(type === 'video' ? 'projects.selectedVideo' : 'projects.selectedMusic')}
        </span>
      </header>
      {material ? (
        <>
          <h3>{material.name}</h3>
          <StatusBadge status={material.condition} />
        </>
      ) : (
        <p>{t(type === 'video' ? 'projects.noVideo' : 'projects.noMusic')}</p>
      )}
      {!editable ? (
        <Link className="text-link" to={appPaths.materials}>
          {t('common.viewDetails')}
        </Link>
      ) : null}
    </article>
  )
}

export function ProjectOverview() {
  const { t } = useTranslation('common')
  const { workspace, refetch } = useProjectContext()
  const navigate = useNavigate()
  const project = workspace.project
  const saved = project.creative_brief
  const [videoId, setVideoId] = useState(project.video_material_ids[0] ?? '')
  const [musicId, setMusicId] = useState(project.music_material_ids[0] ?? '')
  const [intent, setIntent] = useState(saved?.editing_intent ?? '')
  const [duration, setDuration] = useState(
    saved ? secondsToTime(saved.target_duration_sec) : '',
  )
  const [startGuardOpen, setStartGuardOpen] = useState(false)
  const videos = useQuery({
    queryKey: ['materials', 'video', '', 'name_asc'],
    queryFn: () => api.materials.list('video', '', 'name_asc'),
  })
  const music = useQuery({
    queryKey: ['materials', 'music', '', 'name_asc'],
    queryFn: () => api.materials.list('music', '', 'name_asc'),
  })
  const videoItems = videos.data ? collectionItems(videos.data) : []
  const musicItems = music.data ? collectionItems(music.data) : []
  const selectedVideo = videoItems.find((item) => item.material_id === videoId)
  const selectedMusic = musicItems.find((item) => item.material_id === musicId)
  const seconds = parseTime(duration)
  const musicDuration = selectedMusic?.duration_sec
  const exceedsMusic =
    typeof musicDuration === 'number' &&
    Number.isFinite(seconds) &&
    seconds > musicDuration
  const valid =
    Boolean(videoId) &&
    Boolean(musicId) &&
    intent.trim().length > 0 &&
    Number.isFinite(seconds) &&
    seconds > 0 &&
    !exceedsMusic
  const materialsReady =
    selectedVideo?.condition.toLowerCase() === 'ready' &&
    selectedMusic?.condition.toLowerCase() === 'ready'
  const dirty =
    videoId !== (project.video_material_ids[0] ?? '') ||
    musicId !== (project.music_material_ids[0] ?? '') ||
    intent !== (saved?.editing_intent ?? '') ||
    duration !== (saved ? secondsToTime(saved.target_duration_sec) : '')
  const blocker = useBlocker(dirty)
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (dirty) event.preventDefault()
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])
  const save = useMutation({
    mutationFn: () =>
      api.projects.saveSetup(project.project_id, [videoId], [musicId], {
        editing_intent: intent.trim(),
        target_duration_sec: seconds,
      }),
    onSuccess: async () => {
      await refetch()
    },
  })
  const start = useMutation({
    mutationFn: () => api.projects.startRun(project.project_id),
    onSuccess: async (submission) => {
      await refetch()
      navigate(appRoutes.runDetail(project.project_id, submission.run.run_id))
    },
  })
  return (
    <div className="project-section project-setup">
      <header className="section-header">
        <div>
          <span className="eyebrow">{t('projects.workspaceEyebrow')}</span>
          <h2>{t('projects.setup')}</h2>
          <p>{t('projects.setupSubtitle')}</p>
        </div>
        {dirty ? <span className="dirty-label">{t('common.unsaved')}</span> : null}
      </header>
      {videos.isPending || music.isPending ? <LoadingState /> : null}
      {videos.isError || music.isError ? (
        <ErrorState
          onRetry={() => {
            void videos.refetch()
            void music.refetch()
          }}
        />
      ) : null}
      {!videos.isPending && !music.isPending ? (
        <form
          className="project-setup__form"
          onSubmit={(event) => {
            event.preventDefault()
            if (valid) save.mutate()
          }}
        >
          <section className="setup-card setup-card--materials">
            <header>
              <div>
                <span className="setup-step">01</span>
                <h3>{t('projects.materials')}</h3>
              </div>
              <p>{t('projects.materialsHelp')}</p>
            </header>
            <div className="setup-material-grid">
              <label className="field">
                <span>{t('projects.selectedVideo')}</span>
                <select value={videoId} onChange={(e) => setVideoId(e.target.value)}>
                  <option value="">{t('projects.noVideo')}</option>
                  {videoItems
                    .filter((item) => item.condition.toLowerCase() !== 'inconsistent')
                    .map((item) => (
                      <option key={item.material_id} value={item.material_id}>
                        {item.name} · {item.condition}
                      </option>
                    ))}
                </select>
              </label>
              <label className="field">
                <span>{t('projects.selectedMusic')}</span>
                <select value={musicId} onChange={(e) => setMusicId(e.target.value)}>
                  <option value="">{t('projects.noMusic')}</option>
                  {musicItems
                    .filter((item) => item.condition.toLowerCase() !== 'inconsistent')
                    .map((item) => (
                      <option key={item.material_id} value={item.material_id}>
                        {item.name} · {item.condition}
                      </option>
                    ))}
                </select>
              </label>
            </div>
          </section>
          <section className="setup-card setup-card--brief">
            <header>
              <div>
                <span className="setup-step">02</span>
                <h3>{t('brief.title')}</h3>
              </div>
              <p>{t('brief.subtitle')}</p>
            </header>
            <label className="field">
              <span>{t('projects.editingIntent')}</span>
              <textarea
                rows={6}
                required
                value={intent}
                onChange={(event) => setIntent(event.target.value)}
              />
              <small>{t('brief.intentHelp')}</small>
            </label>
            <label className="field field--duration" htmlFor="setup-target-duration">
              <span>{t('projects.targetDuration')}</span>
              <input
                id="setup-target-duration"
                aria-invalid={exceedsMusic}
                disabled={!musicId}
                value={duration}
                onChange={(event) => setDuration(event.target.value)}
                placeholder="01:00"
              />
              <small className={exceedsMusic ? 'field-error' : undefined}>
                {musicId ? t('brief.durationHelp') : t('brief.requiresMusic')}
                {exceedsMusic && musicDuration
                  ? ` (${secondsToTime(musicDuration)})`
                  : ''}
              </small>
            </label>
          </section>
          <aside className="setup-card setup-card--master">
            <header>
              <div>
                <span className="setup-step">03</span>
                <h3>MASTER</h3>
              </div>
              <p>{t('projects.masterReady')}</p>
            </header>
            <div className="master-lane" aria-label="MASTER">
              {['M', 'A', 'S', 'T', 'E', 'R'].map((role, index) => (
                <span
                  className={index === 0 ? 'master-role master-role--m' : 'master-role'}
                  key={role}
                >
                  {role}
                </span>
              ))}
            </div>
            {!materialsReady && videoId && musicId ? (
              <p className="setup-warning">{t('projects.materialsNotReady')}</p>
            ) : null}
            {save.isError || start.isError ? <ErrorState /> : null}
            <footer className="project-setup__actions">
              <button
                className="button button--secondary"
                type="submit"
                disabled={!dirty || !valid || save.isPending}
              >
                <Save size={16} />
                {t('projects.saveSetup')}
              </button>
              <button
                className="button button--primary button--start"
                type="button"
                disabled={!valid || !materialsReady || start.isPending}
                onClick={() => {
                  if (dirty) setStartGuardOpen(true)
                  else start.mutate()
                }}
              >
                <Rocket size={17} />
                {start.isPending ? t('projects.starting') : t('projects.startEditing')}
              </button>
            </footer>
          </aside>
        </form>
      ) : null}
      {blocker.state === 'blocked' || startGuardOpen ? (
        <div className="dialog-layer">
          <div className="dialog">
            <header>
              <h2>{t('projects.leaveSetupTitle')}</h2>
            </header>
            <p>{t('projects.leaveSetupBody')}</p>
            <footer>
              <button
                className="button button--primary"
                type="button"
                onClick={() => {
                  setStartGuardOpen(false)
                  if (blocker.state === 'blocked') blocker.reset()
                }}
              >
                {t('common.back')}
              </button>
            </footer>
          </div>
        </div>
      ) : null}
    </div>
  )
}

export function ProjectMaterials() {
  const { t } = useTranslation('common')
  const { workspace, refetch } = useProjectContext()
  const [videoId, setVideoId] = useState(workspace.project.video_material_ids[0] ?? '')
  const [musicId, setMusicId] = useState(workspace.project.music_material_ids[0] ?? '')
  const videos = useQuery({
    queryKey: ['materials', 'video', '', 'name_asc'],
    queryFn: () => api.materials.list('video', '', 'name_asc'),
  })
  const music = useQuery({
    queryKey: ['materials', 'music', '', 'name_asc'],
    queryFn: () => api.materials.list('music', '', 'name_asc'),
  })
  const save = useMutation({
    mutationFn: () =>
      api.projects.setMaterials(
        workspace.project.project_id,
        videoId ? [videoId] : [],
        musicId ? [musicId] : [],
      ),
    onSuccess: async () => {
      await refetch()
    },
  })
  const videoItems = videos.data
    ? Array.isArray(videos.data)
      ? videos.data
      : videos.data.items
    : []
  const musicItems = music.data
    ? Array.isArray(music.data)
      ? music.data
      : music.data.items
    : []
  const changed =
    videoId !== (workspace.project.video_material_ids[0] ?? '') ||
    musicId !== (workspace.project.music_material_ids[0] ?? '')
  const blocker = useBlocker(changed)
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (changed) event.preventDefault()
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [changed])
  return (
    <div className="project-section">
      <header className="section-header">
        <div>
          <span className="eyebrow">{t('projects.materialReferences')}</span>
          <h2>{t('projects.materials')}</h2>
        </div>
      </header>
      <div className="summary-pair">
        <MaterialPanel type="video" material={workspace.materials?.video[0]} editable />
        <MaterialPanel type="music" material={workspace.materials?.music[0]} editable />
      </div>
      {videos.isPending || music.isPending ? <LoadingState /> : null}
      {videos.isError || music.isError ? (
        <ErrorState
          onRetry={() => {
            void videos.refetch()
            void music.refetch()
          }}
        />
      ) : null}
      {!videos.isPending && !music.isPending ? (
        <form
          className="material-picker"
          onSubmit={(event) => {
            event.preventDefault()
            save.mutate()
          }}
        >
          <label className="field">
            <span>{t('projects.selectedVideo')}</span>
            <select
              value={videoId}
              onChange={(event) => setVideoId(event.target.value)}
            >
              <option value="">{t('projects.noVideo')}</option>
              {videoItems
                .filter((item) => item.condition.toLowerCase() !== 'inconsistent')
                .map((item) => (
                  <option key={item.material_id} value={item.material_id}>
                    {item.name} · {item.condition}
                  </option>
                ))}
            </select>
          </label>
          <label className="field">
            <span>{t('projects.selectedMusic')}</span>
            <select
              value={musicId}
              onChange={(event) => setMusicId(event.target.value)}
            >
              <option value="">{t('projects.noMusic')}</option>
              {musicItems
                .filter((item) => item.condition.toLowerCase() !== 'inconsistent')
                .map((item) => (
                  <option key={item.material_id} value={item.material_id}>
                    {item.name} · {item.condition}
                  </option>
                ))}
            </select>
          </label>
          {save.isError ? <ErrorState /> : null}
          <div className="form-actions">
            <button
              className="button button--primary"
              type="submit"
              disabled={!changed || save.isPending}
            >
              <Save size={16} />
              {t('common.save')}
            </button>
          </div>
        </form>
      ) : null}
      {blocker.state === 'blocked' ? (
        <div className="dialog-layer">
          <div className="dialog">
            <header>
              <h2>{t('projects.leaveMaterialsTitle')}</h2>
            </header>
            <p>{t('projects.leaveMaterialsBody')}</p>
            <footer>
              <button
                className="button button--primary"
                type="button"
                onClick={() => blocker.reset()}
              >
                {t('common.back')}
              </button>
            </footer>
          </div>
        </div>
      ) : null}
    </div>
  )
}

function secondsToTime(value: number) {
  const seconds = Math.max(0, Math.round(value))
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`
}

function parseTime(value: string) {
  const normalized = value.trim()
  if (/^\d+$/.test(normalized)) return Number(normalized)
  const match = /^(\d+):(\d{1,2})$/.exec(normalized)
  if (!match) return Number.NaN
  return Number(match[1]) * 60 + Number(match[2])
}

export function CreativeBrief() {
  const { t } = useTranslation('common')
  const { workspace } = useProjectContext()
  const queryClient = useQueryClient()
  const project = workspace.project
  const saved = project.creative_brief
  const [intent, setIntent] = useState(saved?.editing_intent ?? '')
  const [duration, setDuration] = useState(
    saved ? secondsToTime(saved.target_duration_sec) : '',
  )
  const dirty =
    intent !== (saved?.editing_intent ?? '') ||
    duration !== (saved ? secondsToTime(saved.target_duration_sec) : '')
  const seconds = parseTime(duration)
  const hasMusic = Boolean(workspace.materials?.music.length)
  const musicDuration = workspace.materials?.music[0]?.duration_sec
  const exceedsMusic =
    typeof musicDuration === 'number' &&
    Number.isFinite(seconds) &&
    seconds > musicDuration
  const valid =
    intent.trim().length > 0 &&
    hasMusic &&
    Number.isFinite(seconds) &&
    seconds > 0 &&
    !exceedsMusic
  const blocker = useBlocker(dirty)
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (dirty) event.preventDefault()
    }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty])
  const save = useMutation({
    mutationFn: () =>
      api.projects.saveBrief(project.project_id, {
        editing_intent: intent.trim(),
        target_duration_sec: seconds,
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: ['project-workspace', project.project_id],
      })
    },
  })
  return (
    <div className="project-section project-section--form">
      <header className="section-header">
        <div>
          <span className="eyebrow">{t('projects.creativeDirection')}</span>
          <h2>{t('brief.title')}</h2>
          <p>{t('brief.subtitle')}</p>
        </div>
        {dirty ? <span className="dirty-label">{t('common.unsaved')}</span> : null}
      </header>
      <form
        className="brief-form"
        onSubmit={(event) => {
          event.preventDefault()
          if (valid) save.mutate()
        }}
      >
        <label className="field">
          <span>{t('projects.editingIntent')}</span>
          <textarea
            rows={8}
            required
            value={intent}
            onChange={(event) => setIntent(event.target.value)}
          />
          <small>{t('brief.intentHelp')}</small>
        </label>
        <label className="field field--duration" htmlFor="target-duration">
          <span>{t('projects.targetDuration')}</span>
          <input
            id="target-duration"
            aria-invalid={exceedsMusic}
            disabled={!hasMusic}
            value={duration}
            onChange={(event) => setDuration(event.target.value)}
            placeholder="01:00"
          />
          <small className={exceedsMusic ? 'field-error' : undefined}>
            {hasMusic ? t('brief.durationHelp') : t('brief.requiresMusic')}
            {exceedsMusic && musicDuration ? ` (${secondsToTime(musicDuration)})` : ''}
          </small>
        </label>
        {save.isError ? <ErrorState /> : null}
        <div className="form-actions">
          <button
            className="button button--primary"
            type="submit"
            disabled={!dirty || !valid || save.isPending}
          >
            <Save size={16} />
            {t('brief.saveChanges')}
          </button>
        </div>
      </form>
      {blocker.state === 'blocked' ? (
        <div className="dialog-layer">
          <div className="dialog">
            <header>
              <h2>{t('brief.leaveTitle')}</h2>
            </header>
            <p>{t('brief.leaveBody')}</p>
            <footer>
              <button
                className="button button--primary"
                type="button"
                onClick={() => blocker.reset()}
              >
                {t('common.back')}
              </button>
            </footer>
          </div>
        </div>
      ) : null}
    </div>
  )
}

export function ProjectRuns() {
  const { t } = useTranslation('common')
  const { workspace } = useProjectContext()
  const projectId = workspace.project.project_id
  const runsQuery = useQuery({
    queryKey: ['project-runs', projectId],
    queryFn: () => api.runs.list(projectId),
    enabled: Boolean(projectId),
    refetchInterval: (query) => {
      const value = query.state.data
      return value && hasActiveRun(collectionItems(value)) ? 2000 : false
    },
  })
  if (runsQuery.isPending) return <LoadingState />
  if (runsQuery.isError) {
    return <ErrorState onRetry={() => void runsQuery.refetch()} />
  }
  const runs = collectionItems(runsQuery.data)
  return (
    <div className="project-section">
      <header className="section-header">
        <div>
          <span className="eyebrow">{t('projects.immutableHistory')}</span>
          <h2>{t('projects.runs')}</h2>
        </div>
      </header>
      {runs.length === 0 ? (
        <section className="empty-panel">
          <Layers3 size={28} />
          <h3>{t('projects.noRuns')}</h3>
        </section>
      ) : (
        <div className="history-list">
          {runs.map(({ run, execution }) => {
            const active = isRunExecutionActive(run.status, execution)
            return (
              <Link
                className="history-list__link run-list-card"
                key={run.run_id}
                to={appRoutes.runDetail(projectId, run.run_id)}
              >
                <span className="run-list-card__header">
                  <strong>
                    {t('projects.runSequence', { sequence: run.sequence })}
                  </strong>
                  <StatusBadge status={executionStatus(run.status, execution)} />
                </span>
                {active ? (
                  <AsterProgress compact execution={execution} runStatus={run.status} />
                ) : null}
              </Link>
            )
          })}
        </div>
      )}
    </div>
  )
}

export function ProjectOutputs() {
  const { t } = useTranslation('common')
  const { workspace } = useProjectContext()
  return (
    <CollectionPage
      icon={PlaySquare}
      title={t('projects.outputs')}
      empty={t('projects.noOutputs')}
      items={workspace.outputs ?? []}
    />
  )
}

function CollectionPage({
  icon: Icon,
  title,
  empty,
  items,
}: {
  icon: LucideIcon
  title: string
  empty: string
  items: Array<{
    status: string
    run_id?: string
    render_id?: string
    variant_id?: string
  }>
}) {
  const { t } = useTranslation('common')
  return (
    <div className="project-section">
      <header className="section-header">
        <div>
          <span className="eyebrow">{t('projects.immutableHistory')}</span>
          <h2>{title}</h2>
        </div>
      </header>
      {items.length === 0 ? (
        <section className="empty-panel">
          <Icon size={28} />
          <h3>{empty}</h3>
        </section>
      ) : (
        <div className="history-list">
          {items.map((item) => (
            <article key={item.run_id ?? item.render_id ?? item.variant_id}>
              <span>{item.run_id ?? item.render_id ?? item.variant_id}</span>
              <StatusBadge status={item.status} />
            </article>
          ))}
        </div>
      )}
    </div>
  )
}

export function RunDetail() {
  const { t } = useTranslation('common')
  const { runId = '' } = useParams()
  const detail = useQuery({
    queryKey: ['run', runId],
    queryFn: () => api.runs.get(runId),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      const value = query.state.data
      return value && isRunExecutionActive(value.run.status, value.execution)
        ? 2000
        : false
    },
  })
  if (detail.isPending) return <LoadingState />
  if (detail.isError) return <ErrorState onRetry={() => void detail.refetch()} />
  const run = detail.data.run
  const execution = detail.data.execution
  const active = isRunExecutionActive(run.status, execution)
  const displayStatus = executionStatus(run.status, execution)
  return (
    <div className="project-section run-detail">
      <header className="section-header">
        <div>
          <span className="eyebrow">ASTER</span>
          <h2>{t('projects.runSequence', { sequence: run.sequence })}</h2>
          <p>{run.creative_brief.editing_intent}</p>
        </div>
        <StatusBadge status={displayStatus} />
      </header>
      <section className="run-detail__grid">
        <article className="summary-panel">
          <span>{t('projects.targetDuration')}</span>
          <strong>{secondsToTime(run.creative_brief.target_duration_sec)}</strong>
        </article>
        <article className="summary-panel">
          <span>{t('projects.frozenEdits')}</span>
          <strong>{detail.data.frozen_edits.length}</strong>
        </article>
      </section>
      {run.failure_message ? (
        <section className="run-failure">
          <h3>{t('common.errorTitle')}</h3>
          <p>{run.failure_message}</p>
        </section>
      ) : null}
      {active || ['complete', 'completed'].includes(run.status) ? (
        <AsterProgress execution={execution} runStatus={run.status} />
      ) : null}
      {detail.data.frozen_edits.length > 0 ? (
        <div className="history-list">
          {detail.data.frozen_edits.map((edit) => (
            <Link
              className="history-list__link"
              key={edit.edit_id}
              to={appRoutes.review(run.project_id, run.run_id, edit.edit_id)}
            >
              <span>{t('projects.editSequence', { sequence: edit.sequence })}</span>
              <StatusBadge status="complete" />
            </Link>
          ))}
        </div>
      ) : null}
    </div>
  )
}

export function ReviewUnavailable() {
  const { t } = useTranslation('common')
  const navigate = useNavigate()
  return (
    <div className="page">
      <section className="empty-panel">
        <PlaySquare size={30} />
        <h1>{t('common.unavailable')}</h1>
        <p>{t('common.notAvailable')}</p>
        <button
          className="button button--secondary"
          type="button"
          onClick={() => navigate(-1)}
        >
          {t('common.back')}
        </button>
      </section>
    </div>
  )
}
