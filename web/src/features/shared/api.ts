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

export type MaterialReference =
  | {
      kind: 'project_current'
      project_name: string
      navigation: {
        kind: 'project'
        project_id: string
      }
    }
  | {
      kind: 'run_snapshot'
      project_name: string
      run_sequence: number
      navigation: {
        kind: 'run'
        project_id: string
        run_id: string
      }
    }
  | {
      kind: 'unknown'
      navigation: null
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
  references?: MaterialReference[]
  attempts?: AttemptSummary[]
  latest_execution?: ExecutionSummary | null
}

export interface MaterialPreflightResult {
  available: boolean
  normalized_name: string
  existing_material?: MaterialSummary | null
}

export interface MaterialSubmission {
  material: MaterialSummary
  attempt: AttemptSummary
  job: JobSummary
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
  selected_materials?: {
    video: MaterialSummary[]
    music: MaterialSummary[]
  }
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
  log?: AttemptLogInfo
}

export interface AttemptLogInfo {
  path: string
  exists: boolean
}

export interface AttemptLogEntry {
  cursor: number
  timestamp: string | null
  level: 'DEBUG' | 'INFO' | 'SUCCESS' | 'WARNING' | 'ERROR' | 'CRITICAL' | 'RAW'
  component: string | null
  event: string | null
  fields: string
  message: string
}

export interface AttemptLogPage {
  attempt_id: string
  log: AttemptLogInfo
  entries: AttemptLogEntry[]
  start_cursor: number
  end_cursor: number
  has_more_before: boolean
}

export interface ModelUsageBucket {
  request_count: number
  reported_usage_count: number
  unreported_usage_count: number
  priced_usage_count: number
  unpriced_usage_count: number
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
  cached_prompt_tokens: number
  uncached_prompt_tokens: number
  reasoning_tokens: number
  uncached_input_cost_yuan: number
  cached_input_cost_yuan: number
  output_cost_yuan: number
  total_cost_yuan: number
}

export interface CompactModelUsageSummary extends ModelUsageBucket {
  currency: 'CNY'
  price_unit: 'yuan_per_million_tokens'
}

export interface ModelUsageSummary extends CompactModelUsageSummary {
  by_model: Record<string, ModelUsageBucket>
  by_task: Record<string, ModelUsageBucket>
}

export interface AttemptModelUsage {
  attempt_id: string
  sequence: number
  status: string
  model_usage_summary: ModelUsageSummary | null
}

export interface RunModelUsage {
  schema_version: '1.0'
  attempt_usage: AttemptModelUsage[]
  run_total: ModelUsageSummary
}

export interface RunListItem {
  run: RunSummary
  execution: ExecutionSummary | null
  model_usage_total: CompactModelUsageSummary
}

export interface RunDetailView {
  run: RunSummary
  frozen_edits: FrozenEditSummary[]
  execution: ExecutionSummary | null
  model_usage: RunModelUsage
}

export type RenderAudioMode = 'dialogue' | 'bgm_only'

export type RenderVariantStatus =
  'queued' | 'rendering' | 'ready' | 'failed' | 'interrupted' | 'unavailable'

export interface RenderSpecification extends Record<string, unknown> {
  schema_version: string
  audio_mode: RenderAudioMode
  renderer: {
    width: number
    height: number
    fps: number
    encoder: string
    threads: number
    bgm_volume: number
    original_volume: number
    audio_sample_rate: number
    dialogue_audio: Record<string, unknown>
    [key: string]: unknown
  }
}

export interface RenderVariant {
  render_variant_id: string
  project_id: string
  run_id: string
  run_sequence: number
  edit_id: string
  edit_sequence: number
  edit_origin: 'initial' | 'guided_revision'
  status: RenderVariantStatus
  specification: RenderSpecification
  frame_count: number | null
  duration_sec: number | null
  size_bytes: number | null
  failure_message: string | null
  media_url: string | null
  download_url: string | null
  created_at: string
  updated_at: string
}

export interface RenderVariantSubmission {
  render_variant: RenderVariant
  execution: ExecutionSummary | null
  created: boolean
}

export interface RenderVariantListItem {
  render_variant: RenderVariant
  execution: ExecutionSummary | null
}

export interface RenderVariantCollection {
  items: RenderVariantListItem[]
  count: number
}

export interface RenderIntegrity {
  state: 'verified'
  cached: boolean
  size_bytes: number
}

export interface RenderVerification {
  render_variant: RenderVariant
  integrity: RenderIntegrity
}

export interface DeletedRenderVariant {
  render_variant_id: string
  deleted: true
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

export type ReviewRenderVariant = RenderVariant

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
  outputs?: RenderVariantListItem[]
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
      material_name: string
    }
  | {
      type: 'run'
      project_id: string
      project_name: string
      run_id: string
      run_sequence: number
    }
  | {
      type: 'render_variant'
      project_id: string
      project_name: string
      run_id: string
      run_sequence: number
      edit_id: string
      edit_version: number
      render_variant_id: string
      audio_mode: RenderAudioMode
    }

export interface ActivityItem {
  attempt: AttemptSummary
  job: JobSummary
  navigation: ActivityNavigation | null
}

export interface ActivityCollection {
  items: ActivityItem[]
  limit: number
  offset: number
  has_more: boolean
}

export interface SettingsView {
  values: Record<string, unknown>
  base_path: string
  data_root: string
  secrets: {
    llm_configured: boolean
    vlm_configured: boolean
    asr_configured: boolean
  }
  connections: ProviderSettings
}

export type ProviderCapability = 'llm' | 'vlm' | 'asr'
export type ProviderProfile = 'cost_saving' | 'simple' | 'custom'

export interface ModelProviderConfiguration {
  model: string
  base_url: string
  api_key_env: string
  enable_thinking: boolean
  temperature: number
  max_tokens: number
  timeout_sec: number
  max_retries: number
  max_concurrency: number
  input_price_yuan_per_million_tokens: number
  cached_input_price_yuan_per_million_tokens: number
  output_price_yuan_per_million_tokens: number
}

export interface AsrProviderConfiguration {
  backend: 'bailian'
  api_key_env: string
  reuse: boolean
  timeout_sec: number
  poll_interval_sec: number
  max_chars: number
  max_subtitle_duration_sec: number
}

export interface ProviderBundle {
  llm: ModelProviderConfiguration
  vlm: ModelProviderConfiguration
  asr: AsrProviderConfiguration
}

export interface CredentialStatus {
  configured: boolean
  suffix: string | null
  source: 'process' | 'dotenv' | 'none'
  writable: boolean
}

export interface ProviderSettings {
  profile: ProviderProfile
  providers: ProviderBundle
  presets: {
    cost_saving: ProviderBundle
    simple: ProviderBundle
  }
  credentials: Record<ProviderCapability, CredentialStatus>
}

export type CredentialUpdate =
  { action: 'keep' } | { action: 'set'; value: string } | { action: 'clear' }

export interface SaveProviderSettingsPayload {
  profile: ProviderProfile
  providers?: ProviderBundle
  credentials: Record<ProviderCapability, CredentialUpdate>
}

export interface SavedProviderSettings {
  settings: SettingsView
  restart_required: boolean
  credential_results: Record<
    ProviderCapability,
    'kept' | 'set' | 'cleared' | 'process_locked'
  >
}

export interface ProviderConnectionResult {
  capability: ProviderCapability
  status: 'connected'
  latency_ms: number
}

export interface StorageReport {
  data_root: string
  categories: Array<{ name: string; file_count: number; size_bytes: number }>
  direct_bundle_count: number
  total_size_bytes: number
  reveal_supported: boolean
}

export type DataRootMigrationStatus =
  | 'requested'
  | 'quiescing'
  | 'copying'
  | 'verifying'
  | 'cancelling'
  | 'rolling_back'
  | 'cancelled'
  | 'failed'
  | 'switching'
  | 'restart_required'
  | 'complete'

export interface DataRootMigrationBlocker {
  kind: string
  metadata: Record<string, unknown>
}

export interface DataRootMigrationPreflight {
  source_root: string
  destination_root: string
  eligible: boolean
  estimated_file_count: number
  estimated_size_bytes: number
  blockers: DataRootMigrationBlocker[]
}

export interface DataRootMigration {
  migration_id: string
  source_root: string
  destination_root: string
  status: DataRootMigrationStatus
  progress: {
    phase: string
    files_completed: number
    files_total: number
    bytes_completed: number
    bytes_total: number
  }
  cancel_requested: boolean
  blockers: DataRootMigrationBlocker[]
  failure: { code: string } | null
  created_at: string
  updated_at: string
  started_at: string | null
  finished_at: string | null
}

export interface HealthView {
  status: 'ok' | 'restart_required'
  service: 'cutmaster'
  data_root?: {
    maintenance: boolean
    restart_required: boolean
    migration_id: string | null
    migration_status: DataRootMigrationStatus | null
  }
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
  limit: number
  offset: number
  payload: Record<string, unknown>
}

export interface MaterialMemoryQuery {
  limit: number
  offset: number
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
  const hasJsonBody =
    init.body !== undefined &&
    !(typeof FormData !== 'undefined' && init.body instanceof FormData)
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: 'application/json',
      ...(hasJsonBody ? { 'Content-Type': 'application/json' } : {}),
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
    memory: (
      materialId: string,
      tab: string,
      { limit, offset }: MaterialMemoryQuery = { limit: 100, offset: 0 },
    ) => {
      const params = new URLSearchParams({
        limit: String(limit),
        offset: String(offset),
      })
      return apiRequest<MaterialMemoryResponse>(
        `/api/materials/${encodeURIComponent(materialId)}/memory/${encodeURIComponent(tab)}?${params.toString()}`,
      )
    },
    preflight: (materialType: MaterialType, name: string) =>
      apiRequest<MaterialPreflightResult>('/api/materials/preflight', {
        method: 'POST',
        body: JSON.stringify({ material_type: materialType, name }),
      }),
    create: ({
      materialType,
      name,
      source,
      subtitle,
    }: {
      materialType: MaterialType
      name: string
      source: File
      subtitle?: File | null
    }) => {
      const form = new FormData()
      form.set('material_type', materialType)
      form.set('name', name)
      form.set('source', source)
      if (materialType === 'video' && subtitle) form.set('subtitle', subtitle)
      return apiRequest<MaterialSubmission>('/api/materials', {
        method: 'POST',
        headers: commandHeaders(),
        body: form,
      })
    },
    retry: (materialId: string) =>
      apiRequest<MaterialSubmission>(
        `/api/materials/${encodeURIComponent(materialId)}/retry`,
        { method: 'POST', headers: commandHeaders() },
      ),
    resume: (materialId: string) =>
      apiRequest<MaterialSubmission>(
        `/api/materials/${encodeURIComponent(materialId)}/resume`,
        { method: 'POST', headers: commandHeaders() },
      ),
    delete: (materialId: string) =>
      apiRequest<void>(`/api/materials/${encodeURIComponent(materialId)}`, {
        method: 'DELETE',
        headers: commandHeaders(),
      }),
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
    rename: (projectId: string, name: string) =>
      apiRequest<ProjectSummary>(
        `/api/projects/${encodeURIComponent(projectId)}/rename`,
        {
          method: 'POST',
          headers: commandHeaders(),
          body: JSON.stringify({ name }),
        },
      ),
    delete: (projectId: string) =>
      apiRequest<void>(`/api/projects/${encodeURIComponent(projectId)}`, {
        method: 'DELETE',
        headers: commandHeaders(),
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
    retry: (runId: string) =>
      apiRequest<RunSubmission>(`/api/runs/${encodeURIComponent(runId)}/retry`, {
        method: 'POST',
        headers: commandHeaders(),
      }),
    resume: (runId: string) =>
      apiRequest<RunSubmission>(`/api/runs/${encodeURIComponent(runId)}/resume`, {
        method: 'POST',
        headers: commandHeaders(),
      }),
    runAgain: (runId: string) =>
      apiRequest<RunSubmission>(`/api/runs/${encodeURIComponent(runId)}/run-again`, {
        method: 'POST',
        headers: commandHeaders(),
      }),
    delete: (runId: string) =>
      apiRequest<{ run_id: string; deleted: boolean }>(
        `/api/runs/${encodeURIComponent(runId)}`,
        { method: 'DELETE', headers: commandHeaders() },
      ),
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
  renderVariants: {
    listForEdit: (editId: string) =>
      apiRequest<RenderVariantCollection>(
        `/api/frozen-edits/${encodeURIComponent(editId)}/render-variants`,
      ),
    listForProject: (projectId: string) =>
      apiRequest<RenderVariantCollection>(
        `/api/projects/${encodeURIComponent(projectId)}/render-variants`,
      ),
    create: (editId: string, audioMode: RenderAudioMode) =>
      apiRequest<RenderVariantSubmission>(
        `/api/frozen-edits/${encodeURIComponent(editId)}/render-variants`,
        {
          method: 'POST',
          headers: commandHeaders(),
          body: JSON.stringify({ audio_mode: audioMode }),
        },
      ),
    get: (renderVariantId: string) =>
      apiRequest<RenderVariantListItem>(
        `/api/render-variants/${encodeURIComponent(renderVariantId)}`,
      ),
    retry: (renderVariantId: string) =>
      apiRequest<RenderVariantSubmission>(
        `/api/render-variants/${encodeURIComponent(renderVariantId)}/retry`,
        { method: 'POST', headers: commandHeaders() },
      ),
    resume: (renderVariantId: string) =>
      apiRequest<RenderVariantSubmission>(
        `/api/render-variants/${encodeURIComponent(renderVariantId)}/resume`,
        { method: 'POST', headers: commandHeaders() },
      ),
    renderAgain: (renderVariantId: string) =>
      apiRequest<RenderVariantSubmission>(
        `/api/render-variants/${encodeURIComponent(renderVariantId)}/render-again`,
        { method: 'POST', headers: commandHeaders() },
      ),
    verify: (renderVariantId: string) =>
      apiRequest<RenderVerification>(
        `/api/render-variants/${encodeURIComponent(renderVariantId)}/verify`,
        { method: 'POST', headers: commandHeaders() },
      ),
    delete: (renderVariantId: string) =>
      apiRequest<DeletedRenderVariant>(
        `/api/render-variants/${encodeURIComponent(renderVariantId)}`,
        { method: 'DELETE', headers: commandHeaders() },
      ),
    mediaUrl: (renderVariantId: string) =>
      `/api/render-variants/${encodeURIComponent(renderVariantId)}/media`,
    downloadUrl: (renderVariantId: string) =>
      `/api/render-variants/${encodeURIComponent(renderVariantId)}/download`,
  },
  activity: {
    list: (offset = 0) => {
      const path =
        offset === 0
          ? '/api/activity'
          : `/api/activity?${new URLSearchParams({
              limit: '100',
              offset: String(offset),
            }).toString()}`
      return apiRequest<ActivityCollection>(path)
    },
  },
  attempts: {
    logs: (attemptId: string) =>
      apiRequest<AttemptLogPage>(`/api/attempts/${encodeURIComponent(attemptId)}/logs`),
    logStreamUrl: (attemptId: string, afterCursor: number) =>
      `/api/attempts/${encodeURIComponent(attemptId)}/logs/stream?${new URLSearchParams(
        {
          after_cursor: String(afterCursor),
        },
      ).toString()}`,
    stop: (attemptId: string) =>
      apiRequest<{ attempt: AttemptSummary; job: Record<string, unknown> }>(
        `/api/attempts/${encodeURIComponent(attemptId)}/stop`,
        { method: 'POST', headers: commandHeaders() },
      ),
  },
  settings: {
    get: () => apiRequest<SettingsView>('/api/settings'),
    saveProviders: (payload: SaveProviderSettingsPayload) =>
      apiRequest<SavedProviderSettings>('/api/settings/providers', {
        method: 'PUT',
        headers: commandHeaders(),
        body: JSON.stringify(payload),
      }),
    testProvider: (
      capability: ProviderCapability,
      configuration: ModelProviderConfiguration | AsrProviderConfiguration,
      apiKey?: string,
    ) =>
      apiRequest<ProviderConnectionResult>(
        `/api/settings/providers/${capability}/test`,
        {
          method: 'POST',
          body: JSON.stringify({
            configuration,
            ...(apiKey ? { api_key: apiKey } : {}),
          }),
        },
      ),
    storage: () => apiRequest<StorageReport>('/api/settings/storage'),
    revealStorage: () =>
      apiRequest<{ opened: boolean }>('/api/settings/storage/reveal', {
        method: 'POST',
        headers: commandHeaders(),
      }),
    preflightDataRootMigration: (destinationRoot: string) =>
      apiRequest<DataRootMigrationPreflight>(
        '/api/settings/storage/migrations/preflight',
        {
          method: 'POST',
          body: JSON.stringify({ destination_root: destinationRoot }),
        },
      ),
    startDataRootMigration: (destinationRoot: string) =>
      apiRequest<DataRootMigration>('/api/settings/storage/migrations', {
        method: 'POST',
        headers: commandHeaders(),
        body: JSON.stringify({ destination_root: destinationRoot }),
      }),
    currentDataRootMigration: () =>
      apiRequest<{ migration: DataRootMigration | null }>(
        '/api/settings/storage/migrations/current',
      ),
    dataRootMigration: (migrationId: string) =>
      apiRequest<DataRootMigration>(
        `/api/settings/storage/migrations/${encodeURIComponent(migrationId)}`,
      ),
    cancelDataRootMigration: (migrationId: string) =>
      apiRequest<DataRootMigration>(
        `/api/settings/storage/migrations/${encodeURIComponent(migrationId)}/cancel`,
        { method: 'POST', headers: commandHeaders() },
      ),
  },
}
