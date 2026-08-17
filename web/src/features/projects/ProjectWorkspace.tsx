import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AudioWaveform,
  ChevronLeft,
  Film,
  Layers3,
  LoaderCircle,
  RefreshCcw,
  Repeat2,
  Rocket,
  Save,
  Square,
  Trash2,
} from 'lucide-react'
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
import { useEventStream } from '@/app/providers/event-stream-context'
import { ErrorState, LoadingState } from '@/components/ui/AsyncState'
import { OperationProblem } from '@/components/ui/OperationProblem'
import { StatusBadge } from '@/components/ui/StatusBadge'
import { ProjectOutputsView } from '@/features/renders/ProjectOutputs'
import { AsterProgress } from '@/features/shared/AsterProgress'
import {
  api,
  collectionItems,
  type ExecutionSummary,
  type MaterialSummary,
  type ModelUsageBucket,
  type ProjectWorkspace as WorkspaceData,
  type RunModelUsage,
  type RunSummary,
} from '@/features/shared/api'
import {
  executionStatus,
  hasActiveRun,
  isRunExecutionActive,
} from '@/features/shared/execution-state'
import { ExecutionFailure } from '@/features/shared/ExecutionFailure'
import { ExecutionLogAccess } from '@/features/shared/LiveLogDialog'
import { formatCost, formatNumber } from '@/i18n/formatters'

interface ProjectContext {
  workspace: WorkspaceData
  refetch: () => Promise<unknown>
}

type DurationMode = 'custom' | 'music'

interface TargetDurationEditor {
  mode: DurationMode
  minutes: number
  seconds: number
  totalSeconds: number
  maximumSeconds: number | null
  maximumMinutes: number
  maximumSecondsForMinute: number
  hasMusic: boolean
  valid: boolean
  setMode: (mode: DurationMode) => void
  setMinutes: (minutes: number) => void
  setSeconds: (seconds: number) => void
  constrainTo: (duration: number | null | undefined) => void
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

function wholeDurationParts(value: number) {
  const total = Number.isFinite(value) ? Math.max(0, Math.round(value)) : 0
  return {
    minutes: Math.floor(total / 60),
    seconds: total % 60,
  }
}

function boundedInteger(value: number, maximum: number) {
  if (!Number.isFinite(value)) return 0
  return Math.max(0, Math.min(maximum, Math.trunc(value)))
}

function useTargetDurationEditor(
  savedTargetSeconds: number | undefined,
  musicDuration: number | null | undefined,
): TargetDurationEditor {
  const initial = wholeDurationParts(savedTargetSeconds ?? 0)
  const [minutes, updateMinutes] = useState(initial.minutes)
  const [seconds, updateSeconds] = useState(initial.seconds)
  const [selectedMode, setSelectedMode] = useState<DurationMode | null>(null)
  const hasMusic =
    typeof musicDuration === 'number' &&
    Number.isFinite(musicDuration) &&
    musicDuration > 0
  const maximumSeconds = hasMusic ? Math.floor(musicDuration) : null
  const inferredMode: DurationMode =
    hasMusic &&
    typeof savedTargetSeconds === 'number' &&
    Math.abs(savedTargetSeconds - musicDuration) < 0.001
      ? 'music'
      : 'custom'
  const mode = selectedMode ?? inferredMode
  const storedCustomSeconds = minutes * 60 + seconds
  const boundedCustomSeconds =
    maximumSeconds === null
      ? storedCustomSeconds
      : Math.min(storedCustomSeconds, maximumSeconds)
  const boundedCustom = wholeDurationParts(boundedCustomSeconds)
  const maximumMinutes = maximumSeconds === null ? 0 : Math.floor(maximumSeconds / 60)
  const maximumSecondsForMinute =
    maximumSeconds === null
      ? 59
      : boundedCustom.minutes >= maximumMinutes
        ? maximumSeconds - maximumMinutes * 60
        : 59

  const setMinutes = (value: number) => {
    const nextMinutes = boundedInteger(value, maximumMinutes)
    const nextSecondsLimit =
      maximumSeconds === null || nextMinutes < maximumMinutes
        ? 59
        : maximumSeconds - maximumMinutes * 60
    updateMinutes(nextMinutes)
    updateSeconds((current) => Math.min(current, nextSecondsLimit))
  }
  const setSeconds = (value: number) => {
    updateSeconds(boundedInteger(value, maximumSecondsForMinute))
  }
  const constrainTo = (duration: number | null | undefined) => {
    if (typeof duration !== 'number' || !Number.isFinite(duration) || duration <= 0)
      return
    const maximum = Math.floor(duration)
    if (storedCustomSeconds <= maximum) return
    const bounded = wholeDurationParts(maximum)
    updateMinutes(bounded.minutes)
    updateSeconds(bounded.seconds)
  }
  const totalSeconds =
    mode === 'music' && hasMusic ? musicDuration : boundedCustomSeconds
  const valid =
    hasMusic &&
    Number.isFinite(totalSeconds) &&
    totalSeconds > 0 &&
    totalSeconds <= musicDuration

  return {
    mode,
    minutes: boundedCustom.minutes,
    seconds: boundedCustom.seconds,
    totalSeconds,
    maximumSeconds,
    maximumMinutes,
    maximumSecondsForMinute,
    hasMusic,
    valid,
    setMode: setSelectedMode,
    setMinutes,
    setSeconds,
    constrainTo,
  }
}

function TargetDurationField({
  idPrefix,
  editor,
}: {
  idPrefix: string
  editor: TargetDurationEditor
}) {
  const { t } = useTranslation('common')
  const helpId = `${idPrefix}-help`
  return (
    <fieldset className="target-duration-field" aria-describedby={helpId}>
      <legend>{t('projects.targetDuration')}</legend>
      <div className="target-duration-modes">
        <label>
          <input
            type="radio"
            name={`${idPrefix}-mode`}
            value="custom"
            checked={editor.mode === 'custom'}
            disabled={!editor.hasMusic}
            onChange={() => editor.setMode('custom')}
          />
          <span>{t('brief.customDuration')}</span>
        </label>
        <label>
          <input
            type="radio"
            name={`${idPrefix}-mode`}
            value="music"
            checked={editor.mode === 'music'}
            disabled={!editor.hasMusic}
            onChange={() => editor.setMode('music')}
          />
          <span>{t('brief.useMusicDuration')}</span>
        </label>
      </div>
      {editor.mode === 'custom' ? (
        <div className="target-duration-inputs">
          <label htmlFor={`${idPrefix}-minutes`}>
            <span>{t('brief.minutes')}</span>
            <input
              id={`${idPrefix}-minutes`}
              type="number"
              inputMode="numeric"
              min={0}
              max={editor.maximumMinutes}
              step={1}
              value={editor.minutes}
              disabled={!editor.hasMusic}
              onChange={(event) => editor.setMinutes(event.currentTarget.valueAsNumber)}
            />
          </label>
          <span className="target-duration-inputs__separator" aria-hidden="true">
            :
          </span>
          <label htmlFor={`${idPrefix}-seconds`}>
            <span>{t('brief.seconds')}</span>
            <input
              id={`${idPrefix}-seconds`}
              type="number"
              inputMode="numeric"
              min={0}
              max={editor.maximumSecondsForMinute}
              step={1}
              value={editor.seconds}
              disabled={!editor.hasMusic}
              onChange={(event) => editor.setSeconds(event.currentTarget.valueAsNumber)}
            />
          </label>
        </div>
      ) : (
        <div className="target-duration-audio">
          <AudioWaveform size={18} aria-hidden="true" />
          <span>{t('brief.selectedMusicDuration')}</span>
          <strong>{secondsToTime(editor.totalSeconds)}</strong>
        </div>
      )}
      <small id={helpId}>
        {editor.hasMusic
          ? t('brief.durationLimit', {
              duration: secondsToTime(editor.maximumSeconds ?? 0),
            })
          : t('brief.requiresMusic')}
      </small>
    </fieldset>
  )
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
  const musicDuration = selectedMusic?.duration_sec
  const duration = useTargetDurationEditor(saved?.target_duration_sec, musicDuration)
  const seconds = duration.totalSeconds
  const valid =
    Boolean(videoId) && Boolean(musicId) && intent.trim().length > 0 && duration.valid
  const materialsReady =
    selectedVideo?.condition.toLowerCase() === 'ready' &&
    selectedMusic?.condition.toLowerCase() === 'ready'
  const dirty =
    videoId !== (project.video_material_ids[0] ?? '') ||
    musicId !== (project.music_material_ids[0] ?? '') ||
    intent !== (saved?.editing_intent ?? '') ||
    (saved ? Math.abs(seconds - saved.target_duration_sec) >= 0.001 : seconds > 0)
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
                    .filter((item) => item.condition.toLowerCase() === 'ready')
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
                  onChange={(event) => {
                    duration.setMode(duration.mode)
                    duration.constrainTo(
                      musicItems.find((item) => item.material_id === event.target.value)
                        ?.duration_sec,
                    )
                    setMusicId(event.target.value)
                  }}
                >
                  <option value="">{t('projects.noMusic')}</option>
                  {musicItems
                    .filter((item) => item.condition.toLowerCase() === 'ready')
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
            <TargetDurationField idPrefix="setup-target-duration" editor={duration} />
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
            {save.isError || start.isError ? (
              <OperationProblem error={save.error ?? start.error} />
            ) : null}
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
                .filter((item) => item.condition.toLowerCase() === 'ready')
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
                .filter((item) => item.condition.toLowerCase() === 'ready')
                .map((item) => (
                  <option key={item.material_id} value={item.material_id}>
                    {item.name} · {item.condition}
                  </option>
                ))}
            </select>
          </label>
          {save.isError ? <OperationProblem error={save.error} /> : null}
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

function RunExecutionTime({
  run,
  execution,
  compact = false,
}: {
  run: RunSummary
  execution: ExecutionSummary | null
  compact?: boolean
}) {
  const { t } = useTranslation('common')
  const status = executionStatus(run.status, execution).trim().toLowerCase()
  const running = ['running', 'retrying', 'stopping'].includes(status)
  const complete = ['complete', 'completed'].includes(run.status.trim().toLowerCase())
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!running) return
    const timer = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(timer)
  }, [running])

  if (!running && !complete) return null
  const startedAt = Date.parse(execution?.attempt.started_at ?? run.created_at)
  const finishedAt = complete
    ? Date.parse(execution?.attempt.finished_at ?? run.updated_at)
    : now
  if (!Number.isFinite(startedAt) || !Number.isFinite(finishedAt)) return null
  const elapsed = secondsToTime(Math.max(0, (finishedAt - startedAt) / 1000))
  const label = running ? t('projects.runningDuration') : t('projects.runDuration')

  return compact ? (
    <span className="run-list-card__duration">
      {label} · {elapsed}
    </span>
  ) : (
    <article className="summary-panel">
      <span>{label}</span>
      <strong>{elapsed}</strong>
    </article>
  )
}

export function CreativeBrief() {
  const { t } = useTranslation('common')
  const { workspace } = useProjectContext()
  const queryClient = useQueryClient()
  const project = workspace.project
  const saved = project.creative_brief
  const [intent, setIntent] = useState(saved?.editing_intent ?? '')
  const musicDuration = workspace.materials?.music[0]?.duration_sec
  const duration = useTargetDurationEditor(saved?.target_duration_sec, musicDuration)
  const seconds = duration.totalSeconds
  const dirty =
    intent !== (saved?.editing_intent ?? '') ||
    (saved ? Math.abs(seconds - saved.target_duration_sec) >= 0.001 : seconds > 0)
  const valid = intent.trim().length > 0 && duration.valid
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
        <TargetDurationField idPrefix="target-duration" editor={duration} />
        {save.isError ? <OperationProblem error={save.error} /> : null}
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

function RunActions({
  run,
  execution,
  compact = false,
}: {
  run: RunSummary
  execution: ExecutionSummary | null
  compact?: boolean
}) {
  const { t } = useTranslation('common')
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [deleteArmed, setDeleteArmed] = useState(false)
  const status = executionStatus(run.status, execution).trim().toLocaleLowerCase()
  const active = isRunExecutionActive(run.status, execution)
  const terminal = ['failed', 'interrupted', 'complete', 'completed'].includes(status)
  const stop = useMutation({
    mutationFn: () => {
      if (!execution) throw new Error('Active Run has no execution Attempt')
      return api.attempts.stop(execution.attempt.attempt_id)
    },
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['run', run.run_id] }),
        queryClient.invalidateQueries({ queryKey: ['project-runs', run.project_id] }),
        queryClient.invalidateQueries({ queryKey: ['projects'] }),
        queryClient.invalidateQueries({ queryKey: ['activity'] }),
      ])
    },
  })
  const recovery = useMutation({
    mutationFn: async (action: 'retry' | 'resume' | 'runAgain') => {
      if (action === 'retry') return api.runs.retry(run.run_id)
      if (action === 'resume') return api.runs.resume(run.run_id)
      return api.runs.runAgain(run.run_id)
    },
    onSuccess: async (submission) => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['project-runs', run.project_id] }),
        queryClient.invalidateQueries({ queryKey: ['run', submission.run.run_id] }),
        queryClient.invalidateQueries({
          queryKey: ['project-workspace', run.project_id],
        }),
        queryClient.invalidateQueries({ queryKey: ['projects'] }),
        queryClient.invalidateQueries({ queryKey: ['activity'] }),
      ])
      navigate(appRoutes.runDetail(run.project_id, submission.run.run_id))
    },
  })
  const remove = useMutation({
    mutationFn: () => api.runs.delete(run.run_id),
    onSuccess: async () => {
      queryClient.removeQueries({ queryKey: ['run', run.run_id] })
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['project-runs', run.project_id] }),
        queryClient.invalidateQueries({
          queryKey: ['project-workspace', run.project_id],
        }),
        queryClient.invalidateQueries({ queryKey: ['projects'] }),
        queryClient.invalidateQueries({ queryKey: ['activity'] }),
      ])
      navigate(appRoutes.projectRuns(run.project_id), { replace: true })
    },
  })

  useEffect(() => {
    if (!deleteArmed) return
    const timeout = window.setTimeout(() => setDeleteArmed(false), 6000)
    return () => window.clearTimeout(timeout)
  }, [deleteArmed])

  if (active) {
    if (compact || !execution) return null
    const stopping = execution.attempt.status.trim().toLocaleLowerCase() === 'stopping'
    return (
      <div className="run-actions">
        <div className="run-actions__buttons">
          <button
            className="button button--secondary"
            type="button"
            disabled={stopping || stop.isPending}
            onClick={() => stop.mutate()}
          >
            {stop.isPending ? (
              <LoaderCircle className="spin" size={15} aria-hidden="true" />
            ) : (
              <Square size={14} aria-hidden="true" />
            )}
            {stopping ? t('projects.stoppingRun') : t('projects.stopRun')}
          </button>
        </div>
        {stop.isError ? <OperationProblem error={stop.error} /> : null}
      </div>
    )
  }
  if (!terminal) return null
  const pending = recovery.isPending || remove.isPending
  return (
    <div className={compact ? 'run-actions run-actions--compact' : 'run-actions'}>
      <div className="run-actions__buttons">
        {status === 'failed' ? (
          <button
            className="button button--secondary"
            type="button"
            disabled={pending}
            onClick={() => recovery.mutate('retry')}
          >
            {recovery.isPending ? (
              <LoaderCircle className="spin" size={15} aria-hidden="true" />
            ) : (
              <RefreshCcw size={15} aria-hidden="true" />
            )}
            {t('projects.retryRun')}
          </button>
        ) : null}
        {status === 'interrupted' ? (
          <button
            className="button button--secondary"
            type="button"
            disabled={pending}
            onClick={() => recovery.mutate('resume')}
          >
            {recovery.isPending ? (
              <LoaderCircle className="spin" size={15} aria-hidden="true" />
            ) : (
              <RefreshCcw size={15} aria-hidden="true" />
            )}
            {t('projects.resumeRun')}
          </button>
        ) : null}
        {['complete', 'completed'].includes(status) ? (
          <button
            className="button button--secondary"
            type="button"
            disabled={pending}
            onClick={() => recovery.mutate('runAgain')}
          >
            {recovery.isPending ? (
              <LoaderCircle className="spin" size={15} aria-hidden="true" />
            ) : (
              <Repeat2 size={15} aria-hidden="true" />
            )}
            {t('projects.runAgain')}
          </button>
        ) : null}
        <button
          className={deleteArmed ? 'button button--danger' : 'button button--secondary'}
          type="button"
          disabled={pending}
          aria-label={
            deleteArmed
              ? t('projects.confirmDeleteRun', { sequence: run.sequence })
              : t('projects.deleteRun', { sequence: run.sequence })
          }
          onClick={() => {
            recovery.reset()
            if (deleteArmed) remove.mutate()
            else setDeleteArmed(true)
          }}
        >
          {remove.isPending ? (
            <LoaderCircle className="spin" size={15} aria-hidden="true" />
          ) : (
            <Trash2 size={15} aria-hidden="true" />
          )}
          {deleteArmed ? t('projects.confirmDelete') : t('projects.delete')}
        </button>
      </div>
      {deleteArmed && !remove.isPending ? (
        <small className="run-actions__confirm">{t('projects.deleteRunAgain')}</small>
      ) : null}
      {recovery.isError ? <OperationProblem error={recovery.error} /> : null}
      {remove.isError ? <OperationProblem error={remove.error} /> : null}
    </div>
  )
}

export function ProjectRuns() {
  const { t, i18n } = useTranslation('common')
  const { workspace } = useProjectContext()
  const { isConnected } = useEventStream()
  const projectId = workspace.project.project_id
  const runsQuery = useQuery({
    queryKey: ['project-runs', projectId],
    queryFn: () => api.runs.list(projectId),
    enabled: Boolean(projectId),
    refetchInterval: (query) => {
      if (isConnected) return false
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
          {runs.map(({ run, execution, model_usage_total: usage }) => {
            const active = isRunExecutionActive(run.status, execution)
            return (
              <article className="run-list-item" key={run.run_id}>
                <Link
                  className="history-list__link run-list-card"
                  to={appRoutes.runDetail(projectId, run.run_id)}
                >
                  <span className="run-list-card__header">
                    <strong>
                      {t('projects.runSequence', { sequence: run.sequence })}
                    </strong>
                    <StatusBadge status={executionStatus(run.status, execution)} />
                  </span>
                  <RunExecutionTime run={run} execution={execution} compact />
                  {active ? (
                    <AsterProgress
                      compact
                      execution={execution}
                      runStatus={run.status}
                    />
                  ) : null}
                  {usage && usage.request_count > 0 ? (
                    <span className="run-list-card__usage">
                      {t('projects.usageCompact', {
                        tokens: formatNumber(usage.total_tokens, i18n.language),
                        cost: formatCost(usage.total_cost_yuan, i18n.language),
                      })}
                    </span>
                  ) : null}
                </Link>
                <RunActions run={run} execution={execution} compact />
              </article>
            )
          })}
        </div>
      )}
    </div>
  )
}

export function ProjectOutputs() {
  return <ProjectOutputsView />
}

export function RunDetail() {
  const { t } = useTranslation('common')
  const { runId = '' } = useParams()
  const { isConnected } = useEventStream()
  const detail = useQuery({
    queryKey: ['run', runId],
    queryFn: () => api.runs.get(runId),
    enabled: Boolean(runId),
    refetchInterval: (query) => {
      if (isConnected) return false
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
        <RunExecutionTime run={run} execution={execution} />
      </section>
      {detail.data.model_usage ? (
        <RunUsagePanel modelUsage={detail.data.model_usage} />
      ) : null}
      {run.failure_message ? (
        <section className="run-failure">
          <h3>{t('common.errorTitle')}</h3>
          <ExecutionFailure
            operationType="aster_planning"
            ownerType="run"
            status={run.status}
          />
        </section>
      ) : null}
      {active || ['complete', 'completed'].includes(run.status) ? (
        <AsterProgress execution={execution} runStatus={run.status} />
      ) : null}
      <RunActions run={run} execution={execution} />
      <ExecutionLogAccess execution={execution} className="run-detail__logs" />
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

function RunUsagePanel({ modelUsage }: { modelUsage: RunModelUsage }) {
  const { t, i18n } = useTranslation('common')
  const total = modelUsage.run_total
  const modelEntries = Object.entries(total.by_model)
  const taskEntries = Object.entries(total.by_task)
  return (
    <section className="run-usage" aria-labelledby="run-usage-title">
      <header className="run-usage__header">
        <div>
          <span className="eyebrow">ASTER</span>
          <h3 id="run-usage-title">{t('projects.usageTitle')}</h3>
          <p>{t('projects.usageSubtitle')}</p>
        </div>
        <strong>{formatCost(total.total_cost_yuan, i18n.language)}</strong>
      </header>
      <div className="run-usage__metrics">
        <UsageMetric label={t('projects.usageRequests')} value={total.request_count} />
        <UsageMetric
          label={t('projects.usageReportedRequests')}
          value={total.reported_usage_count}
        />
        <UsageMetric
          label={t('projects.usageUnreported')}
          value={total.unreported_usage_count}
        />
        <UsageMetric
          label={t('projects.usageTokens')}
          value={formatNumber(total.total_tokens, i18n.language)}
        />
        <UsageMetric
          label={t('projects.usageCost')}
          value={formatCost(total.total_cost_yuan, i18n.language)}
        />
      </div>
      {total.unreported_usage_count > 0 ? (
        <p className="run-usage__notice" role="status">
          {t('projects.usageUnreportedNotice', {
            count: total.unreported_usage_count,
          })}
        </p>
      ) : null}
      {total.unpriced_usage_count > 0 ? (
        <p className="run-usage__notice" role="status">
          {t('projects.usageUnpricedNotice', {
            count: total.unpriced_usage_count,
          })}
        </p>
      ) : null}
      <section className="run-usage__attempts">
        <h4>{t('projects.usageAttempts')}</h4>
        <div>
          {modelUsage.attempt_usage.map((attempt) => {
            const summary = attempt.model_usage_summary
            return (
              <article key={attempt.attempt_id}>
                <header>
                  <strong>
                    {t('projects.usageAttempt', { sequence: attempt.sequence })}
                  </strong>
                  <StatusBadge status={attempt.status} />
                </header>
                {summary ? (
                  summary.request_count === 0 ? (
                    <small>{t('projects.usageNoRequests')}</small>
                  ) : (
                    <>
                      <span>
                        {t('projects.usageBreakdown', {
                          requests: summary.request_count,
                          tokens: formatNumber(summary.total_tokens, i18n.language),
                          cost: formatCost(summary.total_cost_yuan, i18n.language),
                        })}
                      </span>
                      <small>
                        {t('projects.usageReported', {
                          reported: summary.reported_usage_count,
                          total: summary.request_count,
                        })}
                      </small>
                    </>
                  )
                ) : (
                  <small>
                    {['queued', 'running', 'retrying', 'stopping'].includes(
                      attempt.status.toLowerCase(),
                    )
                      ? t('projects.usagePending')
                      : t('projects.usageUnavailable')}
                  </small>
                )}
              </article>
            )
          })}
        </div>
      </section>
      {modelEntries.length > 0 || taskEntries.length > 0 ? (
        <div className="run-usage__groups">
          <UsageBreakdown title={t('projects.usageByModel')} items={modelEntries} />
          <UsageBreakdown
            title={t('projects.usageByTask')}
            items={taskEntries}
            showAsterAgent
          />
        </div>
      ) : null}
    </section>
  )
}

function UsageMetric({ label, value }: { label: string; value: number | string }) {
  return (
    <article>
      <span>{label}</span>
      <strong>{value}</strong>
    </article>
  )
}

function UsageBreakdown({
  title,
  items,
  showAsterAgent = false,
}: {
  title: string
  items: Array<[string, ModelUsageBucket]>
  showAsterAgent?: boolean
}) {
  const { t, i18n } = useTranslation('common')
  if (items.length === 0) return null
  return (
    <section>
      <h4>{title}</h4>
      <div>
        {items.map(([name, usage]) => (
          <article key={name}>
            <div className="run-usage__group-name">
              {showAsterAgent ? <AsterAgentBadge task={name} /> : null}
              <strong>{name}</strong>
            </div>
            <span>
              {t('projects.usageBreakdown', {
                requests: usage.request_count,
                tokens: formatNumber(usage.total_tokens, i18n.language),
                cost: formatCost(usage.total_cost_yuan, i18n.language),
              })}
            </span>
          </article>
        ))}
      </div>
    </section>
  )
}

const asterAgentByTask: Record<string, { initial: string; name: string }> = {
  slot_arrangement: { initial: 'A', name: 'Arrangement Architect' },
  arrangement_architect: { initial: 'A', name: 'Arrangement Architect' },
  dialogue_anchor_selection: { initial: 'S', name: 'Story Editor' },
  story_editor: { initial: 'S', name: 'Story Editor' },
  candidate_retrieval: { initial: 'T', name: 'Timeline Scout' },
  candidate_visual_scoring: { initial: 'T', name: 'Timeline Scout' },
  timeline_scout: { initial: 'T', name: 'Timeline Scout' },
  pairwise_scoring: { initial: 'E', name: 'Edit Composer' },
  edit_composer: { initial: 'E', name: 'Edit Composer' },
  script_review: { initial: 'R', name: 'Revision Editor' },
  revision_editor: { initial: 'R', name: 'Revision Editor' },
}

function AsterAgentBadge({ task }: { task: string }) {
  const agent = asterAgentByTask[task]
  if (!agent) return null
  return (
    <abbr className="run-usage__agent-badge" title={agent.name} aria-label={agent.name}>
      {agent.initial}
    </abbr>
  )
}
