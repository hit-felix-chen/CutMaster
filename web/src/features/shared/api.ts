export type MaterialType = 'video' | 'music'

export interface ProblemDetails {
  type?: string
  title?: string
  status?: number
  detail?: string
  code?: string
  parameters?: Record<string, unknown>
  field_errors?: unknown[]
  blockers?: unknown[]
  retryable?: boolean
}

export class ApiError extends Error {
  readonly status: number
  readonly problem: ProblemDetails | null

  constructor(status: number, problem: ProblemDetails | null) {
    super(problem?.detail ?? problem?.title ?? `HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.problem = problem
  }
}

export interface MaterialSummary {
  material_id: string
  material_type: MaterialType
  name: string
  condition: string
  reused?: boolean
  duration_sec?: number | null
  reference_count?: number
  thumbnail_url?: string | null
  waveform_url?: string | null
  created_at?: string
  updated_at?: string
}

export interface MaterialDetail extends MaterialSummary {
  analysis_available: boolean
  source?: {
    filename?: string
    size_bytes?: number
    media_type?: string
    width?: number
    height?: number
    frame_rate?: number
  } | null
  memory_summary?: Record<string, unknown> | null
  references?: Array<
    | string
    | {
        project_id?: string
        project_name?: string
        run_id?: string | null
        run_label?: string | null
      }
  >
  attempts?: AttemptSummary[]
}

export interface CreativeBrief {
  editing_intent: string
  target_duration_sec: number
}

export interface ProjectSummary {
  project_id: string
  name: string
  video_material_ids: string[]
  music_material_ids: string[]
  creative_brief: CreativeBrief | null
  created_at: string
  updated_at: string
  latest_run_state?: string | null
  preview_url?: string | null
}

export interface RunSummary {
  run_id: string
  project_id: string
  sequence: number
  status: string
  creative_brief: CreativeBrief
  video_material_ids: string[]
  music_material_ids: string[]
  failure_message?: string | null
  created_at: string
  updated_at: string
}

export type AsterAgent =
  | 'arrangement_architect'
  | 'story_editor'
  | 'timeline_scout'
  | 'edit_composer'
  | 'revision_editor'

export type AsterMilestoneState = 'queued' | 'running' | 'complete'

export interface AsterMilestone {
  id: string
  agent: AsterAgent
  state: AsterMilestoneState
}

interface JobProgressBase {
  schema_version: '1.0'
  phase: 'planners'
  completed: number
  total: 5
  unit: 'agent'
  milestones: AsterMilestone[]
}

export type JobProgress = JobProgressBase &
  (
    | { agent: null; state: 'preparing' }
    | { agent: AsterAgent; state: 'running' | 'complete' }
  )

export interface JobSummary {
  job_id: string
  attempt_id: string
  status: string
  stop_requested: boolean
  worker_id?: string | null
  process_id?: number | null
  heartbeat_at?: string | null
  progress: JobProgress | Record<string, unknown>
  created_at: string
  updated_at: string
}

export interface FrozenEditSummary {
  edit_id: string
  run_id: string
  sequence: number
  origin: string
  parent_edit_id?: string | null
  created_at: string
}

export interface RunSubmission {
  run: RunSummary
  attempt: AttemptSummary
  job: JobSummary
}

export interface ExecutionSummary {
  attempt: AttemptSummary
  job: JobSummary
}

export interface RunListItem {
  run: RunSummary
  execution: ExecutionSummary | null
}

export interface RunDetailView {
  run: RunSummary
  frozen_edits: FrozenEditSummary[]
  execution: ExecutionSummary | null
}

export interface RenderSummary {
  render_id?: string
  variant_id?: string
  frozen_edit_id?: string
  status: string
  audio_mode?: string
  output_url?: string | null
  created_at?: string
}

export interface ReviewPlanSummary {
  schema_version: string
  plan_id: string
  fps: number
  total_frames: number
  duration_sec: number
}

export interface ReviewMediaBinding {
  material_id: string
  source_url: string
}

export interface ReviewSlot {
  slot_id: string
  position: number
  is_anchor: boolean
  output_start_sec: number
  output_end_sec: number
  source_start_sec: number
  source_end_sec: number
  source_timestamp: string
  selected_candidate_id: string
  picture: string
  selection_scores: Record<string, number>
  dialogue_anchor: Record<string, unknown> | null
}

export interface ReviewCandidate {
  candidate_id: string
  slot_id: string
  source_start_sec: number
  source_end_sec: number
  source_timestamp: string
  description: string
  semantic_relevance: number | null
  visual_score: number | null
  protagonist_visibility_score: number | null
  emotional_intensity: number | null
  kinetic_energy: number | null
  salience: number | null
  visual_evidence: string | null
  selected: boolean
  eligible_for_replacement: boolean
  media_url: string
}

export interface ReviewRenderVariant {
  render_variant_id: string
  status: string
  specification: Record<string, unknown>
  duration_sec: number | null
  failure_message: string | null
  media_url: string | null
  created_at: string
  updated_at: string
}

export interface ReviewDialogueCue {
  slot_id: string
  start_sec: number
  end_sec: number
  text: string
  speaker?: string | null
}

export interface ReviewTimeline {
  dialogue_cues: ReviewDialogueCue[]
  music_beats_sec: number[]
  music_beats_available: boolean
}

export interface FrozenEditReview {
  edit: FrozenEditSummary
  run: RunSummary
  versions: FrozenEditSummary[]
  plan: ReviewPlanSummary
  media: {
    video: ReviewMediaBinding
    music: ReviewMediaBinding
  }
  candidate_space_available: boolean
  candidate_space_unavailable_reason: string | null
  slots: ReviewSlot[]
  candidates: Record<string, ReviewCandidate[]>
  variants: ReviewRenderVariant[]
  timeline: ReviewTimeline
}

export interface CreateRevisionResult {
  frozen_edit: FrozenEditSummary
  review_url: string
}

export interface ProjectWorkspace {
  project: ProjectSummary
  materials?: {
    video: MaterialSummary[]
    music: MaterialSummary[]
  }
  runs?: RunSummary[]
  outputs?: RenderSummary[]
}

export interface AttemptSummary {
  attempt_id: string
  operation_type: string
  owner_type: string
  owner_id: string
  sequence: number
  status: string
  error_message?: string | null
  created_at: string
  started_at?: string | null
  finished_at?: string | null
  updated_at: string
}

export type ActivityNavigation =
  | {
      type: 'material'
      material_type: MaterialType
      material_id: string
    }
  | {
      type: 'run'
      project_id: string
      run_id: string
    }
  | {
      type: 'render_variant'
      project_id: string
      run_id: string
      edit_id: string
      render_variant_id: string
    }

export interface ActivityItem {
  attempt: AttemptSummary
  job: JobSummary
  navigation: ActivityNavigation | null
}

export interface SettingsView {
  values: Record<string, unknown>
  base_path: string
  overlay_path: string
  data_root: string
  secrets: {
    llm_configured: boolean
    vlm_configured: boolean
    asr_configured: boolean
  }
}

export interface StorageReport {
  data_root: string
  categories: Array<{ name: string; file_count: number; size_bytes: number }>
  direct_bundle_count: number
  total_size_bytes: number
}

export interface HealthView {
  status: 'ok'
  service: 'cutmaster'
  configured: {
    llm: boolean
    vlm: boolean
    asr: boolean
  }
}

export interface MaterialMemoryResponse {
  material_id: string
  material_type: MaterialType
  tab: string
  limit?: number
  offset?: number
  payload: Record<string, unknown>
}

type CollectionEnvelope<T> = { items: T[]; next_cursor?: string | null }

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function collectionItems<T>(value: T[] | CollectionEnvelope<T>): T[] {
  return Array.isArray(value) ? value : value.items
}

async function parseProblem(response: Response): Promise<ProblemDetails | null> {
  try {
    const value: unknown = await response.json()
    return isRecord(value) ? (value as ProblemDetails) : null
  } catch {
    return null
  }
}

export async function apiRequest<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(init.body === undefined ? {} : { 'Content-Type': 'application/json' }),
      ...init.headers,
    },
  })
  if (!response.ok) {
    throw new ApiError(response.status, await parseProblem(response))
  }
  if (response.status === 204) {
    return undefined as T
  }
  return (await response.json()) as T
}

function commandHeaders(): HeadersInit {
  return { 'Idempotency-Key': crypto.randomUUID() }
}

export const api = {
  health: {
    get: () => apiRequest<HealthView>('/api/health'),
  },
  materials: {
    list: (type: MaterialType, search: string, sort: string) => {
      const params = new URLSearchParams({ type, sort })
      if (search) params.set('search', search)
      return apiRequest<MaterialSummary[] | CollectionEnvelope<MaterialSummary>>(
        `/api/materials?${params.toString()}`,
      )
    },
    detail: (materialId: string) =>
      apiRequest<MaterialDetail>(`/api/materials/${encodeURIComponent(materialId)}`),
    memory: (materialId: string, tab: string) =>
      apiRequest<MaterialMemoryResponse>(
        `/api/materials/${encodeURIComponent(materialId)}/memory/${encodeURIComponent(tab)}`,
      ),
  },
  projects: {
    list: () =>
      apiRequest<ProjectSummary[] | CollectionEnvelope<ProjectSummary>>(
        '/api/projects',
      ),
    create: (name: string) =>
      apiRequest<ProjectSummary>('/api/projects', {
        method: 'POST',
        headers: commandHeaders(),
        body: JSON.stringify({ name }),
      }),
    get: (projectId: string) =>
      apiRequest<ProjectSummary>(`/api/projects/${encodeURIComponent(projectId)}`),
    workspace: (projectId: string) =>
      apiRequest<ProjectWorkspace>(
        `/api/projects/${encodeURIComponent(projectId)}/workspace`,
      ),
    saveBrief: (projectId: string, brief: CreativeBrief) =>
      apiRequest<ProjectSummary>(
        `/api/projects/${encodeURIComponent(projectId)}/creative-brief`,
        {
          method: 'PUT',
          headers: commandHeaders(),
          body: JSON.stringify(brief),
        },
      ),
    saveSetup: (
      projectId: string,
      videoMaterialIds: string[],
      musicMaterialIds: string[],
      brief: CreativeBrief,
    ) =>
      apiRequest<ProjectSummary>(
        `/api/projects/${encodeURIComponent(projectId)}/setup`,
        {
          method: 'PUT',
          headers: commandHeaders(),
          body: JSON.stringify({
            video_material_ids: videoMaterialIds,
            music_material_ids: musicMaterialIds,
            ...brief,
          }),
        },
      ),
    startRun: (projectId: string) =>
      apiRequest<RunSubmission>(`/api/projects/${encodeURIComponent(projectId)}/runs`, {
        method: 'POST',
        headers: commandHeaders(),
      }),
    setMaterials: (
      projectId: string,
      videoMaterialIds: string[],
      musicMaterialIds: string[],
    ) =>
      apiRequest<ProjectSummary>(
        `/api/projects/${encodeURIComponent(projectId)}/materials`,
        {
          method: 'PUT',
          headers: commandHeaders(),
          body: JSON.stringify({
            video_material_ids: videoMaterialIds,
            music_material_ids: musicMaterialIds,
          }),
        },
      ),
  },
  runs: {
    list: (projectId: string) =>
      apiRequest<RunListItem[] | CollectionEnvelope<RunListItem>>(
        `/api/projects/${encodeURIComponent(projectId)}/runs`,
      ),
    get: (runId: string) =>
      apiRequest<RunDetailView>(`/api/runs/${encodeURIComponent(runId)}`),
  },
  frozenEdits: {
    review: (editId: string) =>
      apiRequest<FrozenEditReview>(
        `/api/frozen-edits/${encodeURIComponent(editId)}/review`,
      ),
    createRevision: (
      editId: string,
      replacements: Array<{ slot_id: string; candidate_id: string }>,
    ) =>
      apiRequest<CreateRevisionResult>(
        `/api/frozen-edits/${encodeURIComponent(editId)}/revisions`,
        {
          method: 'POST',
          headers: commandHeaders(),
          body: JSON.stringify({ replacements }),
        },
      ),
  },
  activity: {
    list: () =>
      apiRequest<ActivityItem[] | CollectionEnvelope<ActivityItem>>('/api/activity'),
  },
  attempts: {
    stop: (attemptId: string) =>
      apiRequest<{ attempt: AttemptSummary; job: Record<string, unknown> }>(
        `/api/attempts/${encodeURIComponent(attemptId)}/stop`,
        { method: 'POST', headers: commandHeaders() },
      ),
  },
  settings: {
    get: () => apiRequest<SettingsView>('/api/settings'),
    storage: () => apiRequest<StorageReport>('/api/settings/storage'),
  },
}
