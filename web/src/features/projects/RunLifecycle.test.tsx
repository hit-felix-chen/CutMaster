import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { RunDetail } from '@/features/projects/ProjectWorkspace'
import i18n from '@/i18n'

const creativeBrief = {
  editing_intent: 'Preserve the emotional arc.',
  target_duration_sec: 60,
}

function run(status: string) {
  return {
    run_id: 'run_1',
    project_id: 'project_1',
    sequence: 1,
    status,
    creative_brief: creativeBrief,
    video_material_ids: ['mat_video'],
    music_material_ids: ['mat_music'],
    failure_message: status === 'failed' ? 'Provider failed' : null,
    created_at: '2026-08-13T00:00:00Z',
    updated_at: '2026-08-13T00:00:00Z',
  }
}

function execution(status: string) {
  const timestamp = '2026-08-13T00:00:00Z'
  return {
    attempt: {
      attempt_id: 'attempt_1',
      operation_type: 'aster_planning',
      owner_type: 'run',
      owner_id: 'run_1',
      sequence: 1,
      status,
      error_message: status === 'failed' ? 'Provider failed' : null,
      created_at: timestamp,
      started_at: status === 'queued' ? null : timestamp,
      finished_at: ['failed', 'interrupted', 'complete'].includes(status)
        ? timestamp
        : null,
      updated_at: timestamp,
    },
    job: {
      job_id: 'job_1',
      attempt_id: 'attempt_1',
      status,
      stop_requested: status === 'stopping',
      worker_id: status === 'running' ? 'worker-1' : null,
      process_id: status === 'running' ? 42 : null,
      heartbeat_at: status === 'running' ? timestamp : null,
      progress: {},
      created_at: timestamp,
      updated_at: timestamp,
    },
    log: {
      path: '/Users/example/.cutmaster/logs/jobs/job_1.log',
      exists: true,
    },
  }
}

function detail(runStatus: string, attemptStatus = runStatus) {
  return {
    run: run(runStatus),
    frozen_edits: [],
    execution:
      runStatus === 'complete' && attemptStatus === 'complete'
        ? null
        : execution(attemptStatus),
  }
}

function response(value: unknown, status = 200) {
  if (status === 204) return new Response(null, { status })
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function renderRunDetail() {
  const router = createMemoryRouter(
    [
      {
        path: '/projects/:projectId/runs/:runId',
        element: <RunDetail />,
      },
      {
        path: '/projects/:projectId/runs',
        element: <h1>Runs list</h1>,
      },
    ],
    { initialEntries: ['/projects/project_1/runs/run_1'] },
  )
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return { router, client }
}

beforeEach(async () => {
  await i18n.changeLanguage('en-US')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Run lifecycle actions', () => {
  it('shows only Retry for a failed Run and dispatches it idempotently', async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = []
    let current = detail('failed')
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        requests.push({ url, init })
        if (url === '/api/runs/run_1/retry' && init?.method === 'POST') {
          current = detail('queued')
          return response(
            {
              run: current.run,
              attempt: current.execution?.attempt,
              job: current.execution?.job,
            },
            202,
          )
        }
        if (url === '/api/runs/run_1') return response(current)
        return response({ detail: 'Not found' }, 404)
      }),
    )
    renderRunDetail()

    expect(await screen.findByRole('button', { name: 'Retry' })).toBeVisible()
    expect(
      screen.getByText('/Users/example/.cutmaster/logs/jobs/job_1.log'),
    ).toBeVisible()
    expect(screen.getByRole('button', { name: 'View live logs' })).toBeVisible()
    expect(
      screen.getByText(
        'ASTER planning did not complete. Check the project inputs and provider settings, then retry.',
      ),
    ).toHaveAttribute('role', 'alert')
    expect(screen.queryByText('Provider failed')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Resume' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Run again' })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Retry' }))

    await waitFor(() =>
      expect(
        requests.some(
          ({ url, init }) => url.endsWith('/retry') && init?.method === 'POST',
        ),
      ).toBe(true),
    )
    const request = requests.find(({ url }) => url.endsWith('/retry'))
    expect(new Headers(request?.init?.headers).has('Idempotency-Key')).toBe(true)
  })

  it('offers Resume only for an interrupted Run and dispatches it', async () => {
    const requests: string[] = []
    let current = detail('interrupted')
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        if (url === '/api/runs/run_1/resume' && init?.method === 'POST') {
          requests.push(url)
          current = detail('queued')
          return response(
            {
              run: current.run,
              attempt: current.execution?.attempt,
              job: current.execution?.job,
            },
            202,
          )
        }
        if (url === '/api/runs/run_1') return response(current)
        return response({ detail: 'Not found' }, 404)
      }),
    )
    renderRunDetail()

    expect(await screen.findByRole('button', { name: 'Resume' })).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Resume' }))
    await waitFor(() => expect(requests).toEqual(['/api/runs/run_1/resume']))
  })

  it('runs a Complete snapshot again and requires two clicks before delete', async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        requests.push({ url, init })
        if (url === '/api/runs/run_1/run-again' && init?.method === 'POST') {
          const queued = detail('queued')
          return response(
            {
              run: queued.run,
              attempt: queued.execution?.attempt,
              job: queued.execution?.job,
            },
            202,
          )
        }
        if (url === '/api/runs/run_1' && init?.method === 'DELETE') {
          return response({ run_id: 'run_1', deleted: true })
        }
        if (url === '/api/runs/run_1') return response(detail('complete'))
        return response({ detail: 'Not found' }, 404)
      }),
    )
    const { router } = renderRunDetail()

    expect(await screen.findByRole('button', { name: 'Run again' })).toBeVisible()
    await userEvent.click(screen.getByRole('button', { name: 'Run again' }))
    await waitFor(() =>
      expect(requests.some(({ url }) => url.endsWith('/run-again'))).toBe(true),
    )

    await userEvent.click(screen.getByRole('button', { name: 'Delete Edit Run #1' }))
    expect(requests.filter(({ init }) => init?.method === 'DELETE')).toHaveLength(0)
    await userEvent.click(
      screen.getByRole('button', { name: 'Confirm deleting Edit Run #1' }),
    )

    await waitFor(() =>
      expect(requests.filter(({ init }) => init?.method === 'DELETE')).toHaveLength(1),
    )
    await waitFor(() =>
      expect(router.state.location.pathname).toBe('/projects/project_1/runs'),
    )
  })

  it('stops an active Attempt from Run detail and refreshes the Run cache', async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = []
    let current = detail('planners', 'running')
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        requests.push({ url, init })
        if (url === '/api/attempts/attempt_1/stop' && init?.method === 'POST') {
          current = detail('planners', 'stopping')
          return response(current.execution)
        }
        if (url === '/api/runs/run_1') return response(current)
        return response({ detail: 'Not found' }, 404)
      }),
    )
    renderRunDetail()

    await userEvent.click(await screen.findByRole('button', { name: 'Stop attempt' }))

    expect(await screen.findByRole('button', { name: 'Stopping' })).toBeDisabled()
    expect(
      requests.filter(
        ({ url, init }) =>
          url === '/api/attempts/attempt_1/stop' && init?.method === 'POST',
      ),
    ).toHaveLength(1)
    expect(
      requests.filter(({ url, init }) => url === '/api/runs/run_1' && !init?.method)
        .length,
    ).toBeGreaterThanOrEqual(2)
  })
})
