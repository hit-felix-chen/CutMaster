import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ActivityBoard } from '@/features/activity/ActivityBoard'
import {
  ProjectLayout,
  ProjectRuns,
  RunDetail,
} from '@/features/projects/ProjectWorkspace'
import { AsterProgress } from '@/features/shared/AsterProgress'
import i18n from '@/i18n'

function jsonResponse(value: unknown) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const project = {
  project_id: 'project_1',
  name: 'Launch Film',
  video_material_ids: ['mat_video'],
  music_material_ids: ['mat_music'],
  creative_brief: {
    editing_intent: 'Preserve the emotional arc.',
    target_duration_sec: 60,
  },
  created_at: '2026-08-13T00:00:00Z',
  updated_at: '2026-08-13T00:00:00Z',
}

const run = {
  run_id: 'run_1',
  project_id: 'project_1',
  sequence: 1,
  status: 'queued',
  creative_brief: project.creative_brief,
  video_material_ids: ['mat_video'],
  music_material_ids: ['mat_music'],
  failure_message: null,
  created_at: '2026-08-13T00:00:00Z',
  updated_at: '2026-08-13T00:00:00Z',
}

const attempt = {
  attempt_id: 'attempt_1',
  operation_type: 'aster_planning',
  owner_type: 'run',
  owner_id: 'run_1',
  sequence: 1,
  status: 'queued',
  error_message: null,
  created_at: '2026-08-13T00:00:00Z',
  started_at: null,
  finished_at: null,
  updated_at: '2026-08-13T00:00:00Z',
}

const job = {
  job_id: 'job_1',
  attempt_id: 'attempt_1',
  status: 'queued',
  stop_requested: false,
  worker_id: null,
  process_id: null,
  heartbeat_at: null,
  progress: {},
  created_at: '2026-08-13T00:00:00Z',
  updated_at: '2026-08-13T00:00:00Z',
}

const milestones = [
  { id: 'arrangement_architect', agent: 'arrangement_architect', state: 'complete' },
  { id: 'story_editor', agent: 'story_editor', state: 'complete' },
  { id: 'timeline_scout', agent: 'timeline_scout', state: 'running' },
  { id: 'edit_composer', agent: 'edit_composer', state: 'queued' },
  { id: 'revision_editor', agent: 'revision_editor', state: 'queued' },
]

function runningExecution() {
  return {
    attempt: {
      ...attempt,
      status: 'running',
      started_at: '2026-08-13T00:00:01Z',
      updated_at: '2026-08-13T00:00:05Z',
    },
    job: {
      ...job,
      status: 'running',
      worker_id: 'web-run-1',
      process_id: 123,
      heartbeat_at: '2026-08-13T00:00:05Z',
      updated_at: '2026-08-13T00:00:05Z',
      progress: {
        schema_version: '1.0',
        phase: 'planners',
        agent: 'timeline_scout',
        state: 'running',
        completed: 2,
        total: 5,
        unit: 'agent',
        milestones,
      },
    },
  }
}

function queryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Number.POSITIVE_INFINITY },
      mutations: { retry: false },
    },
  })
}

async function flushPromises() {
  await act(async () => {
    for (let index = 0; index < 10; index += 1) {
      await Promise.resolve()
      if (vi.isFakeTimers()) {
        await vi.advanceTimersByTimeAsync(0)
      }
    }
  })
}

beforeEach(async () => {
  await i18n.changeLanguage('en-US')
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('live ASTER execution progress', () => {
  it('polls the lightweight Runs projection from queued through running to terminal', async () => {
    vi.useFakeTimers()
    let runRequests = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input)
        if (url.endsWith('/workspace')) {
          return jsonResponse({
            project,
            materials: { video: [], music: [] },
            runs: [],
          })
        }
        if (url.endsWith('/api/projects/project_1/runs')) {
          runRequests += 1
          if (runRequests === 1) {
            return jsonResponse({
              items: [
                {
                  run: { ...run, status: 'planners' },
                  execution: runningExecution(),
                },
              ],
            })
          }
          return jsonResponse({
            items: [
              {
                run: { ...run, status: 'complete' },
                execution: {
                  ...runningExecution(),
                  attempt: { ...runningExecution().attempt, status: 'complete' },
                  job: { ...runningExecution().job, status: 'complete' },
                },
              },
            ],
          })
        }
        return new Response(null, { status: 404 })
      }),
    )
    const router = createMemoryRouter(
      [
        {
          path: '/projects/:projectId',
          element: <ProjectLayout />,
          children: [{ path: 'runs', element: <ProjectRuns /> }],
        },
      ],
      { initialEntries: ['/projects/project_1/runs'] },
    )
    const client = queryClient()
    client.setQueryData(['project-workspace', 'project_1'], {
      project,
      materials: { video: [], music: [] },
      runs: [],
    })
    client.setQueryData(['project-runs', 'project_1'], {
      items: [{ run, execution: { attempt, job } }],
    })
    render(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(screen.getAllByText('Queued')[0]).toBeVisible()
    expect(runRequests).toBe(0)
    await flushPromises()

    await act(async () => vi.advanceTimersByTimeAsync(2000))
    await flushPromises()
    expect(runRequests).toBe(1)
    expect(client.getQueryData(['project-runs', 'project_1'])).toMatchObject({
      items: [{ run: { status: 'planners' } }],
    })
    await act(async () => vi.advanceTimersByTimeAsync(1))
    expect(screen.getAllByText('Running')[0]).toBeVisible()
    expect(screen.getByText('Timeline Scout is retrieving candidates')).toBeVisible()

    await act(async () => vi.advanceTimersByTimeAsync(2000))
    await flushPromises()
    expect(screen.getByText('Complete')).toBeVisible()
    expect(runRequests).toBe(2)

    await act(async () => vi.advanceTimersByTimeAsync(4000))
    expect(runRequests).toBe(2)
  })

  it('renders planners as Running with the real A/S/T/E/R milestone and heartbeat', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({
          run: { ...run, status: 'planners' },
          frozen_edits: [],
          execution: runningExecution(),
        }),
      ),
    )
    const router = createMemoryRouter(
      [{ path: '/runs/:runId', element: <RunDetail /> }],
      { initialEntries: ['/runs/run_1'] },
    )
    render(
      <QueryClientProvider client={queryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(
      await screen.findByText('Timeline Scout is retrieving candidates'),
    ).toBeVisible()
    expect(screen.getAllByText('Running')[0]).toBeVisible()
    expect(screen.getByText('Arrangement Architect')).toBeVisible()
    expect(screen.getByText('Story Editor')).toBeVisible()
    expect(screen.getByText('Timeline Scout')).toBeVisible()
    expect(screen.getByText('Edit Composer')).toBeVisible()
    expect(screen.getByText('Revision Editor')).toBeVisible()
    expect(screen.getByText(/Heartbeat/)).toBeVisible()
  })

  it('describes a completed ASTER run as complete rather than preparing', () => {
    render(<AsterProgress execution={runningExecution()} runStatus="complete" />)

    expect(screen.getByText('ASTER planning is complete')).toBeVisible()
    expect(
      screen.getByText(
        'All five ASTER agents have finished and the initial Frozen Edit is ready.',
      ),
    ).toBeVisible()
    expect(
      screen.queryByText(
        'The worker has started. The first agent milestone will appear shortly.',
      ),
    ).not.toBeInTheDocument()
  })

  it('keeps an active legacy planners job visibly in ASTER planning', async () => {
    const execution = runningExecution()
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({
          run: { ...run, status: 'planners' },
          frozen_edits: [],
          execution: {
            ...execution,
            job: { ...execution.job, progress: { phase: 'planners' } },
          },
        }),
      ),
    )
    const router = createMemoryRouter(
      [{ path: '/runs/:runId', element: <RunDetail /> }],
      { initialEntries: ['/runs/run_1'] },
    )
    render(
      <QueryClientProvider client={queryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('ASTER is planning the edit')).toBeVisible()
    expect(screen.getAllByText('Running')[0]).toBeVisible()
    expect(
      screen.queryByText('ASTER is preparing the planning workspace'),
    ).not.toBeInTheDocument()
  })

  it('shows the structured pre-agent snapshot as ASTER preparation', async () => {
    const execution = runningExecution()
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({
          run: { ...run, status: 'planners' },
          frozen_edits: [],
          execution: {
            ...execution,
            job: {
              ...execution.job,
              progress: {
                schema_version: '1.0',
                phase: 'planners',
                agent: null,
                state: 'preparing',
                completed: 0,
                total: 5,
                unit: 'agent',
                milestones: milestones.map(({ id, agent }) => ({
                  id,
                  agent,
                  state: 'queued',
                })),
              },
            },
          },
        }),
      ),
    )
    const router = createMemoryRouter(
      [{ path: '/runs/:runId', element: <RunDetail /> }],
      { initialEntries: ['/runs/run_1'] },
    )
    render(
      <QueryClientProvider client={queryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(
      await screen.findByText('ASTER is preparing the planning workspace'),
    ).toBeVisible()
    expect(
      screen.getByText(
        'The worker has started. The first agent milestone will appear shortly.',
      ),
    ).toBeVisible()
  })

  it('polls Activity while active and stops after the Attempt completes', async () => {
    vi.useFakeTimers()
    let activityRequests = 0
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        activityRequests += 1
        if (activityRequests === 1) {
          return jsonResponse({ items: [runningExecution()] })
        }
        return jsonResponse({
          items: [
            {
              attempt: { ...runningExecution().attempt, status: 'complete' },
              job: { ...runningExecution().job, status: 'complete' },
            },
          ],
        })
      }),
    )
    const client = queryClient()
    client.setQueryData(['activity'], { items: [{ attempt, job }] })
    render(
      <QueryClientProvider client={client}>
        <ActivityBoard />
      </QueryClientProvider>,
    )

    expect(screen.getByRole('heading', { name: 'Queued' })).toBeVisible()
    await flushPromises()

    await act(async () => vi.advanceTimersByTimeAsync(2000))
    await flushPromises()
    expect(activityRequests).toBe(1)
    expect(client.getQueryData(['activity'])).toMatchObject({
      items: [{ attempt: { status: 'running' } }],
    })
    await act(async () => vi.advanceTimersByTimeAsync(1))
    expect(screen.getByRole('heading', { name: 'Running' })).toBeVisible()
    expect(screen.getByText('Timeline Scout is retrieving candidates')).toBeVisible()

    await act(async () => vi.advanceTimersByTimeAsync(2000))
    await flushPromises()
    expect(screen.getByRole('heading', { name: 'Recent' })).toBeVisible()
    expect(activityRequests).toBe(2)

    await act(async () => vi.advanceTimersByTimeAsync(4000))
    expect(activityRequests).toBe(2)
  })
})
