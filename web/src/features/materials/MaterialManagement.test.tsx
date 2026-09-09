import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { EventStreamContext } from '@/app/providers/event-stream-context'
import { MaterialsWorkspace } from '@/features/materials/MaterialsWorkspace'
import i18n from '@/i18n'

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function queryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
}

function renderWorkspace(path = '/materials/video', eventStreamConnected = false) {
  const router = createMemoryRouter(
    [{ path: '/materials/:type/:materialId?', element: <MaterialsWorkspace /> }],
    { initialEntries: [path] },
  )
  render(
    <QueryClientProvider client={queryClient()}>
      <EventStreamContext.Provider
        value={{
          connectionState: eventStreamConnected ? 'connected' : 'unsupported',
          isConnected: eventStreamConnected,
          pollingFallback: !eventStreamConnected,
        }}
      >
        <RouterProvider router={router} />
      </EventStreamContext.Provider>
    </QueryClientProvider>,
  )
  return router
}

const attempt = {
  attempt_id: 'attempt_material_1',
  operation_type: 'material_analysis',
  owner_type: 'material',
  owner_id: 'material_1',
  sequence: 1,
  status: 'queued',
  error_message: null,
  created_at: '2026-08-16T00:00:00Z',
  updated_at: '2026-08-16T00:00:00Z',
}

const job = {
  job_id: 'job_material_1',
  attempt_id: attempt.attempt_id,
  status: 'queued',
  stop_requested: false,
  progress: {},
  created_at: attempt.created_at,
  updated_at: attempt.updated_at,
}

const material = {
  material_id: 'material_1',
  material_type: 'video',
  name: 'Opening Number',
  condition: 'queued',
  reference_count: 0,
}

beforeEach(async () => {
  await i18n.changeLanguage('en-US')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Material import and lifecycle actions', () => {
  it('preflights the name and blocks a collision without uploading the source', async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        requests.push({ url, init })
        if (url.startsWith('/api/materials?')) return jsonResponse({ items: [] })
        if (url === '/api/materials/preflight') {
          return jsonResponse({
            available: false,
            normalized_name: 'Opening Number',
            existing_material: { ...material, condition: 'ready' },
          })
        }
        return new Response(null, { status: 404 })
      }),
    )
    const router = renderWorkspace()

    await userEvent.click(
      await screen.findByRole('button', { name: 'Import & Analyse' }),
    )
    await userEvent.type(screen.getByLabelText('Material name'), 'Opening Number')
    await userEvent.upload(
      screen.getByLabelText('Source file'),
      new File(['video'], 'opening.mp4', { type: 'video/mp4' }),
    )
    await userEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', {
        name: 'Import & Analyse',
      }),
    )

    expect(
      await screen.findByText(
        'A material named “Opening Number” already exists in this type.',
      ),
    ).toBeVisible()
    expect(
      requests.filter(({ url }) => url === '/api/materials/preflight'),
    ).toHaveLength(1)
    expect(
      requests.some(
        ({ url, init }) => url === '/api/materials' && init?.method === 'POST',
      ),
    ).toBe(false)
    expect(screen.getByRole('button', { name: 'Change name' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: 'View existing' }))
    expect(router.state.location.pathname).toBe('/materials/video/material_1')
  })

  it('uploads multipart data and opens the queued material with real execution state', async () => {
    let upload: RequestInit | undefined
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        if (url.startsWith('/api/materials?')) return jsonResponse({ items: [] })
        if (url === '/api/materials/preflight') {
          return jsonResponse({ available: true, normalized_name: material.name })
        }
        if (url === '/api/materials' && init?.method === 'POST') {
          upload = init
          return jsonResponse({ material, attempt, job }, 202)
        }
        if (url === '/api/materials/material_1') {
          return jsonResponse({
            ...material,
            analysis_available: false,
            references: [],
            attempts: [attempt],
            latest_execution: {
              attempt,
              job,
              log: {
                path: '/Users/example/.cutmaster/logs/jobs/job_1.log',
                exists: true,
              },
            },
          })
        }
        return new Response(null, { status: 404 })
      }),
    )
    const router = renderWorkspace()

    await userEvent.click(
      await screen.findByRole('button', { name: 'Import & Analyse' }),
    )
    await userEvent.type(screen.getByLabelText('Material name'), material.name)
    await userEvent.upload(
      screen.getByLabelText('Source file'),
      new File(['video'], 'opening.mp4', { type: 'video/mp4' }),
    )
    await userEvent.upload(
      screen.getByLabelText('Subtitle file'),
      new File(['1\n'], 'opening.srt', { type: 'application/x-subrip' }),
    )
    await userEvent.click(
      within(screen.getByRole('dialog')).getByRole('button', {
        name: 'Import & Analyse',
      }),
    )

    await waitFor(() =>
      expect(router.state.location.pathname).toBe('/materials/video/material_1'),
    )
    expect(await screen.findByText('Execution Attempt #1')).toBeVisible()
    expect(
      screen.getByText('/Users/example/.cutmaster/logs/jobs/job_1.log'),
    ).toBeVisible()
    expect(screen.getByRole('button', { name: 'View live logs' })).toBeVisible()
    expect(screen.getAllByText('Queued').length).toBeGreaterThan(0)
    expect(upload?.body).toBeInstanceOf(FormData)
    const form = upload?.body as FormData
    expect(form.get('material_type')).toBe('video')
    expect(form.get('name')).toBe(material.name)
    expect((form.get('source') as File).name).toBe('opening.mp4')
    expect((form.get('subtitle') as File).name).toBe('opening.srt')
    expect(new Headers(upload?.headers).has('Content-Type')).toBe(false)
    expect(new Headers(upload?.headers).has('Idempotency-Key')).toBe(true)
  })

  it('retries a failed analysis and requires two delete clicks for an unreferenced material', async () => {
    const failedAttempt = {
      ...attempt,
      status: 'failed',
      error_message: 'Provider failed',
    }
    const readyMaterial = { ...material, condition: 'failed' }
    const calls: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        if (url.startsWith('/api/materials?')) {
          return jsonResponse({ items: [readyMaterial] })
        }
        if (url === '/api/materials/material_1/retry' && init?.method === 'POST') {
          calls.push('retry')
          return jsonResponse({ material, attempt, job }, 202)
        }
        if (url === '/api/materials/material_1' && init?.method === 'DELETE') {
          calls.push('delete')
          return new Response(null, { status: 204 })
        }
        if (url === '/api/materials/material_1') {
          return jsonResponse({
            ...readyMaterial,
            analysis_available: false,
            references: [],
            attempts: [failedAttempt],
            latest_execution: {
              attempt: failedAttempt,
              job: { ...job, status: 'failed' },
            },
          })
        }
        return new Response(null, { status: 404 })
      }),
    )
    const router = renderWorkspace('/materials/video/material_1')

    expect(router.state.matches[0]?.params.materialId).toBe('material_1')
    expect(
      await screen.findByText(
        'Material analysis did not complete. Check the material and provider settings, then retry.',
      ),
    ).toHaveAttribute('role', 'alert')
    expect(screen.queryByText('Provider failed')).not.toBeInTheDocument()

    await userEvent.click(await screen.findByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(calls).toContain('retry'))

    const deleteButton = screen.getByRole('button', { name: 'Delete material' })
    await userEvent.click(deleteButton)
    expect(calls).not.toContain('delete')
    expect(screen.getByRole('button', { name: 'Click again to delete' })).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Click again to delete' }))

    await waitFor(() => expect(calls).toContain('delete'))
    await waitFor(() => expect(router.state.location.pathname).toBe('/materials/video'))
  })

  it('shows accessible English links for structured references and blocks deletion', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input)
        if (url.startsWith('/api/materials?'))
          return jsonResponse({ items: [material] })
        if (url === '/api/materials/material_1') {
          return jsonResponse({
            ...material,
            condition: 'ready',
            analysis_available: true,
            references: [
              {
                kind: 'project_current',
                project_name: 'lalaland',
                navigation: { kind: 'project', project_id: 'project_1' },
              },
              {
                kind: 'run_snapshot',
                project_name: 'lalaland',
                run_sequence: 1,
                navigation: {
                  kind: 'run',
                  project_id: 'project_1',
                  run_id: 'run_1',
                },
              },
            ],
            attempts: [],
            latest_execution: null,
          })
        }
        return new Response(null, { status: 404 })
      }),
    )
    renderWorkspace('/materials/video/material_1')

    expect(
      await screen.findByRole('button', { name: 'Delete material' }),
    ).toBeDisabled()
    expect(
      screen.getByRole('link', { name: 'Project lalaland current selection' }),
    ).toHaveAttribute('href', '/projects/project_1/overview')
    expect(
      screen.getByRole('link', { name: 'lalaland · Edit Run 1 snapshot' }),
    ).toHaveAttribute('href', '/projects/project_1/runs/run_1')
    expect(screen.queryByText('project_1')).not.toBeInTheDocument()
    expect(screen.queryByText('run_1')).not.toBeInTheDocument()
    expect(screen.getByText(/Remove this material from every project/)).toBeVisible()
  })

  it('localises accessible reference links and a safe unknown fallback in Chinese', async () => {
    await i18n.changeLanguage('zh-CN')
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input)
        if (url.startsWith('/api/materials?'))
          return jsonResponse({ items: [material] })
        if (url === '/api/materials/material_1') {
          return jsonResponse({
            ...material,
            condition: 'ready',
            analysis_available: true,
            references: [
              {
                kind: 'project_current',
                project_name: 'lalaland',
                navigation: { kind: 'project', project_id: 'project_1' },
              },
              {
                kind: 'run_snapshot',
                project_name: 'lalaland',
                run_sequence: 1,
                navigation: {
                  kind: 'run',
                  project_id: 'project_1',
                  run_id: 'run_1',
                },
              },
              { kind: 'unknown', navigation: null },
            ],
            attempts: [],
            latest_execution: null,
          })
        }
        return new Response(null, { status: 404 })
      }),
    )
    renderWorkspace('/materials/video/material_1')

    expect(
      await screen.findByRole('link', { name: '项目 lalaland 当前选择' }),
    ).toHaveAttribute('href', '/projects/project_1/overview')
    expect(
      screen.getByRole('link', { name: 'lalaland · 剪辑运行 1 快照' }),
    ).toHaveAttribute('href', '/projects/project_1/runs/run_1')
    expect(screen.getByText('该引用已不可用')).toBeVisible()
  })

  it('shows measured analysis progress and blocks active deletion', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input)
        if (url.startsWith('/api/materials?')) {
          return jsonResponse({ items: [{ ...material, condition: 'analysing' }] })
        }
        if (url === '/api/materials/material_1') {
          const currentAttempt = {
            ...attempt,
            status: 'running',
            error_message: null,
          }
          return jsonResponse({
            ...material,
            condition: 'analysing',
            analysis_available: false,
            references: [],
            attempts: [currentAttempt],
            latest_execution: {
              attempt: currentAttempt,
              job: {
                ...job,
                status: 'running',
                progress: {
                  schema_version: '1.0',
                  phase: 'analyser',
                  state: 'analysing',
                  completed: 1,
                  total: 3,
                  unit: 'stage',
                },
              },
            },
          })
        }
        return new Response(null, { status: 404 })
      }),
    )
    renderWorkspace('/materials/video/material_1')

    const progress = await screen.findByRole('progressbar')
    expect(progress).toHaveAttribute('aria-valuenow', '1')
    expect(progress).toHaveAttribute('aria-valuemax', '3')
    expect(screen.getByRole('button', { name: 'Delete material' })).toBeDisabled()
    expect(screen.getByText(/active analysis/)).toBeVisible()
  })

  it('shows the analyser node flow and per-node progress for schema 2.0', async () => {
    const nodes = [
      { id: 'shot_detection', state: 'complete', completed: 12, total: 12 },
      { id: 'dialogue_preparation', state: 'complete', completed: 1, total: 1 },
      { id: 'scene_segmentation', state: 'running', completed: 4, total: 10 },
      { id: 'segment_clip_preparation', state: 'queued', completed: 0, total: 10 },
      { id: 'shot_annotation', state: 'queued', completed: 0, total: 12 },
      { id: 'segment_summarization', state: 'queued', completed: 0, total: 10 },
      { id: 'video_summary', state: 'queued', completed: 0, total: 1 },
    ].map((node) => ({ ...node, unit: 'item' }))
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input)
        if (url.startsWith('/api/materials?')) {
          return jsonResponse({ items: [{ ...material, condition: 'analysing' }] })
        }
        if (url === '/api/materials/material_1') {
          const currentAttempt = { ...attempt, status: 'running' }
          return jsonResponse({
            ...material,
            condition: 'analysing',
            analysis_available: false,
            references: [],
            attempts: [currentAttempt],
            latest_execution: {
              attempt: currentAttempt,
              job: {
                ...job,
                status: 'running',
                progress: {
                  schema_version: '2.0',
                  phase: 'analyser',
                  material_type: 'video',
                  state: 'analysing',
                  completed: 2,
                  total: 7,
                  unit: 'node',
                  active_node: 'scene_segmentation',
                  nodes,
                },
              },
            },
          })
        }
        return new Response(null, { status: 404 })
      }),
    )
    renderWorkspace('/materials/video/material_1')

    expect(await screen.findByText('2 / 7 nodes complete')).toBeVisible()
    expect(screen.getByText('Shot detection')).toBeVisible()
    expect(screen.getByText('Video summary')).toBeVisible()
    expect(screen.getByText('Scene segmentation').closest('li')).toHaveAttribute(
      'aria-current',
      'step',
    )
    const activeProgress = screen.getByRole('progressbar', {
      name: 'Scene segmentation progress',
    })
    expect(activeProgress).toHaveAttribute('aria-valuenow', '4')
    expect(activeProgress).toHaveAttribute('aria-valuemax', '10')
    expect(screen.getAllByRole('progressbar')).toHaveLength(7)
    expect(i18n.t('materials.analysisNodes.names.music_analysis')).toBe(
      'Music analysis',
    )
  })

  it('polls active material detail while the event stream is connected', async () => {
    let detailRequests = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input)
        if (url.startsWith('/api/materials?')) {
          return jsonResponse({ items: [{ ...material, condition: 'analysing' }] })
        }
        if (url === '/api/materials/material_1') {
          detailRequests += 1
          const currentAttempt = { ...attempt, status: 'running' }
          return jsonResponse({
            ...material,
            condition: 'analysing',
            analysis_available: false,
            references: [],
            attempts: [currentAttempt],
            latest_execution: {
              attempt: currentAttempt,
              job: { ...job, status: 'running' },
            },
          })
        }
        return new Response(null, { status: 404 })
      }),
    )
    renderWorkspace('/materials/video/material_1', true)

    expect(await screen.findByText('Analysis attempts')).toBeVisible()
    const initialRequests = detailRequests
    expect(initialRequests).toBeGreaterThan(0)
    await waitFor(() => expect(detailRequests).toBeGreaterThan(initialRequests), {
      timeout: 2600,
    })
  })

  it('stops an active analysis Attempt and keeps its stopping state live', async () => {
    let stopRequested = false
    let hasIdempotencyKey = false
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        if (url.startsWith('/api/materials?')) {
          return jsonResponse({ items: [{ ...material, condition: 'analysing' }] })
        }
        if (
          url === '/api/attempts/attempt_material_1/stop' &&
          init?.method === 'POST'
        ) {
          stopRequested = true
          hasIdempotencyKey = new Headers(init.headers).has('Idempotency-Key')
          return jsonResponse({
            attempt: { ...attempt, status: 'stopping' },
            job: { ...job, status: 'running', stop_requested: true },
          })
        }
        if (url === '/api/materials/material_1') {
          const currentAttempt = {
            ...attempt,
            status: stopRequested ? 'stopping' : 'running',
          }
          return jsonResponse({
            ...material,
            condition: 'analysing',
            analysis_available: false,
            references: [],
            attempts: [currentAttempt],
            latest_execution: {
              attempt: currentAttempt,
              job: {
                ...job,
                status: 'running',
                stop_requested: stopRequested,
              },
            },
          })
        }
        return new Response(null, { status: 404 })
      }),
    )
    renderWorkspace('/materials/video/material_1')
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Stop Attempt' }))

    await waitFor(() => expect(stopRequested).toBe(true))
    expect(hasIdempotencyKey).toBe(true)
    expect(await screen.findByRole('button', { name: 'Stopping' })).toBeDisabled()
    expect(screen.getAllByText('Stopping').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: 'Delete material' })).toBeDisabled()
  })

  it('resumes an interrupted analysis as a new real execution', async () => {
    const requests: string[] = []
    const interruptedAttempt = {
      ...attempt,
      status: 'interrupted',
    }
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        if (url.startsWith('/api/materials?')) {
          return jsonResponse({ items: [{ ...material, condition: 'failed' }] })
        }
        if (url === '/api/materials/material_1/resume' && init?.method === 'POST') {
          requests.push('resume')
          return jsonResponse({ material, attempt, job }, 202)
        }
        if (url === '/api/materials/material_1') {
          return jsonResponse({
            ...material,
            condition: 'failed',
            analysis_available: false,
            references: [],
            attempts: [interruptedAttempt],
            latest_execution: {
              attempt: interruptedAttempt,
              job: { ...job, status: 'interrupted' },
            },
          })
        }
        return new Response(null, { status: 404 })
      }),
    )
    renderWorkspace('/materials/video/material_1')

    await userEvent.click(
      await screen.findByRole('button', { name: 'Resume analysis' }),
    )

    await waitFor(() => expect(requests).toContain('resume'))
  })
})
