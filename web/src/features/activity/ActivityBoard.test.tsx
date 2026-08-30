import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, MemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ActivityBoard } from '@/features/activity/ActivityBoard'
import i18n from '@/i18n'

function jsonResponse(value: unknown) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function problemResponse(status: number, detail: string) {
  return new Response(
    JSON.stringify({
      type: 'about:blank',
      title: 'Operation failed',
      status,
      detail,
      code: 'attempt_operation_failed',
    }),
    {
      status,
      headers: { 'Content-Type': 'application/problem+json' },
    },
  )
}

function queryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
}

const attempt = {
  attempt_id: 'attempt_1',
  operation_type: 'aster_planning',
  owner_type: 'run',
  owner_id: 'run_1',
  sequence: 1,
  status: 'complete',
  error_message: null,
  created_at: '2026-08-13T00:00:00Z',
  started_at: '2026-08-13T00:00:01Z',
  finished_at: '2026-08-13T00:01:00Z',
  updated_at: '2026-08-13T00:01:00Z',
}

const job = {
  job_id: 'job_1',
  attempt_id: 'attempt_1',
  status: 'complete',
  stop_requested: false,
  worker_id: 'worker_1',
  process_id: 123,
  heartbeat_at: '2026-08-13T00:01:00Z',
  progress: {},
  created_at: '2026-08-13T00:00:00Z',
  updated_at: '2026-08-13T00:01:00Z',
}

const destinations = [
  {
    name: 'run',
    attempt: { ...attempt, owner_type: 'run', owner_id: 'run_1' },
    navigation: {
      type: 'run',
      project_id: 'project_1',
      project_name: 'Trailer',
      run_id: 'run_1',
      run_sequence: 2,
    },
    pathname: '/projects/project_1/runs/run_1',
    search: '',
    accessibleName: 'Open Run · Trailer · Run 2',
  },
  {
    name: 'material',
    attempt: {
      ...attempt,
      operation_type: 'material_analysis',
      owner_type: 'material',
      owner_id: 'material_1',
    },
    navigation: {
      type: 'material',
      material_type: 'video',
      material_id: 'material_1',
      material_name: 'City footage',
    },
    pathname: '/materials/video/material_1',
    search: '',
    accessibleName: 'Open Material · City footage',
  },
  {
    name: 'render variant',
    attempt: {
      ...attempt,
      operation_type: 'rendering',
      owner_type: 'render_variant',
      owner_id: 'variant_1',
    },
    navigation: {
      type: 'render_variant',
      project_id: 'project_1',
      project_name: 'Trailer',
      run_id: 'run_1',
      run_sequence: 2,
      edit_id: 'edit_1',
      edit_version: 3,
      render_variant_id: 'variant_1',
      audio_mode: 'dialogue',
    },
    pathname: '/projects/project_1/runs/run_1/review/edit_1',
    search: '?variant=variant_1',
    accessibleName: 'Open Render Variant · Trailer · Run 2 · Edit 3 · Dialogue Preview',
  },
] as const

beforeEach(async () => {
  await i18n.changeLanguage('en-US')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Activity owner navigation', () => {
  it.each(destinations)(
    'opens the canonical $name route from a Recent row',
    async ({ attempt: ownerAttempt, navigation, pathname, search, accessibleName }) => {
      vi.stubGlobal(
        'fetch',
        vi.fn(async () =>
          jsonResponse({
            items: [{ attempt: ownerAttempt, job, navigation }],
          }),
        ),
      )
      const router = createMemoryRouter(
        [
          { path: '/activity', element: <ActivityBoard /> },
          { path: '*', element: <div>Destination</div> },
        ],
        { initialEntries: ['/activity'] },
      )
      render(
        <QueryClientProvider client={queryClient()}>
          <RouterProvider router={router} />
        </QueryClientProvider>,
      )

      const link = await screen.findByRole('link', {
        name: accessibleName,
      })
      expect(screen.queryByText(ownerAttempt.owner_id)).not.toBeInTheDocument()
      await userEvent.click(link)

      expect(router.state.location.pathname).toBe(pathname)
      expect(router.state.location.search).toBe(search)
    },
  )

  it('keeps a deleted or unknown owner as an unlinked, UUID-free record', async () => {
    const staleAttempt = {
      ...attempt,
      owner_id: 'run_deleted-private-id',
    }
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({
          items: [{ attempt: staleAttempt, job, navigation: null }],
          limit: 100,
          offset: 0,
          has_more: false,
        }),
      ),
    )
    render(
      <QueryClientProvider client={queryClient()}>
        <MemoryRouter>
          <ActivityBoard />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    expect(await screen.findByText('Run · Deleted or unavailable')).toBeInTheDocument()
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    expect(screen.queryByText('run_deleted-private-id')).not.toBeInTheDocument()
  })

  it('loads the next durable Activity page on demand', async () => {
    const first = {
      attempt: { ...attempt, attempt_id: 'attempt_1', owner_id: 'run_1' },
      job,
      navigation: {
        type: 'run',
        project_id: 'project_1',
        project_name: 'Trailer',
        run_id: 'run_1',
        run_sequence: 1,
      },
    }
    const second = {
      attempt: { ...attempt, attempt_id: 'attempt_2', owner_id: 'run_2' },
      job: { ...job, attempt_id: 'attempt_2' },
      navigation: {
        type: 'run',
        project_id: 'project_1',
        project_name: 'Trailer',
        run_id: 'run_2',
        run_sequence: 2,
      },
    }
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input)
      if (path === '/api/activity') {
        return jsonResponse({
          items: [first],
          limit: 1,
          offset: 0,
          has_more: true,
        })
      }
      if (path === '/api/activity?limit=100&offset=1') {
        return jsonResponse({
          items: [second],
          limit: 100,
          offset: 1,
          has_more: false,
        })
      }
      throw new Error(`Unexpected request: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(
      <QueryClientProvider client={queryClient()}>
        <MemoryRouter>
          <ActivityBoard />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    await userEvent.click(
      await screen.findByRole('button', { name: 'Load more activity' }),
    )

    expect(await screen.findByText('Run · Trailer · Run 2')).toBeInTheDocument()
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/activity?limit=100&offset=1',
      expect.any(Object),
    )
    expect(
      screen.queryByRole('button', { name: 'Load more activity' }),
    ).not.toBeInTheDocument()
  })
})

const recoveryCases = [
  {
    name: 'failed material analysis',
    status: 'failed',
    action: 'Retry',
    ownerType: 'material',
    ownerId: 'material_1',
    operationType: 'material_analysis',
    navigation: {
      type: 'material',
      material_type: 'video',
      material_id: 'material_1',
      material_name: 'City footage',
    },
    endpoint: '/api/materials/material_1/retry',
  },
  {
    name: 'interrupted material analysis',
    status: 'interrupted',
    action: 'Resume',
    ownerType: 'material',
    ownerId: 'material_1',
    operationType: 'material_analysis',
    navigation: {
      type: 'material',
      material_type: 'video',
      material_id: 'material_1',
      material_name: 'City footage',
    },
    endpoint: '/api/materials/material_1/resume',
  },
  {
    name: 'failed ASTER run',
    status: 'failed',
    action: 'Retry',
    ownerType: 'run',
    ownerId: 'run_1',
    operationType: 'aster_planning',
    navigation: {
      type: 'run',
      project_id: 'project_1',
      project_name: 'Trailer',
      run_id: 'run_1',
      run_sequence: 2,
    },
    endpoint: '/api/runs/run_1/retry',
  },
  {
    name: 'interrupted ASTER run',
    status: 'interrupted',
    action: 'Resume',
    ownerType: 'run',
    ownerId: 'run_1',
    operationType: 'aster_planning',
    navigation: {
      type: 'run',
      project_id: 'project_1',
      project_name: 'Trailer',
      run_id: 'run_1',
      run_sequence: 2,
    },
    endpoint: '/api/runs/run_1/resume',
  },
  {
    name: 'failed render',
    status: 'failed',
    action: 'Retry',
    ownerType: 'render_variant',
    ownerId: 'variant_1',
    operationType: 'rendering',
    navigation: {
      type: 'render_variant',
      project_id: 'project_1',
      project_name: 'Trailer',
      run_id: 'run_1',
      run_sequence: 2,
      edit_id: 'edit_1',
      edit_version: 3,
      render_variant_id: 'variant_1',
      audio_mode: 'dialogue',
    },
    endpoint: '/api/render-variants/variant_1/retry',
  },
  {
    name: 'interrupted render',
    status: 'interrupted',
    action: 'Resume',
    ownerType: 'render_variant',
    ownerId: 'variant_1',
    operationType: 'rendering',
    navigation: {
      type: 'render_variant',
      project_id: 'project_1',
      project_name: 'Trailer',
      run_id: 'run_1',
      run_sequence: 2,
      edit_id: 'edit_1',
      edit_version: 3,
      render_variant_id: 'variant_1',
      audio_mode: 'dialogue',
    },
    endpoint: '/api/render-variants/variant_1/resume',
  },
] as const

describe('Activity recovery actions', () => {
  it.each(recoveryCases)(
    'dispatches the real owner endpoint for $name',
    async ({
      status,
      action,
      ownerType,
      ownerId,
      operationType,
      navigation,
      endpoint,
    }) => {
      const item = {
        attempt: {
          ...attempt,
          status,
          owner_type: ownerType,
          owner_id: ownerId,
          operation_type: operationType,
          error_message: status === 'failed' ? 'Provider failed' : null,
        },
        job: { ...job, status },
        navigation,
      }
      const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const path = String(input)
        if ((init?.method ?? 'GET') === 'POST') return jsonResponse({ accepted: true })
        if (path === '/api/activity') return jsonResponse({ items: [item] })
        throw new Error(`Unexpected request: ${path}`)
      })
      vi.stubGlobal('fetch', fetchMock)
      render(
        <QueryClientProvider client={queryClient()}>
          <MemoryRouter>
            <ActivityBoard />
          </MemoryRouter>
        </QueryClientProvider>,
      )

      await userEvent.click(await screen.findByRole('button', { name: action }))

      expect(fetchMock).toHaveBeenCalledWith(
        endpoint,
        expect.objectContaining({ method: 'POST' }),
      )
      const mutationCall = fetchMock.mock.calls.find(
        ([input, init]) => String(input) === endpoint && init?.method === 'POST',
      )
      expect(new Headers(mutationCall?.[1]?.headers).has('Idempotency-Key')).toBe(true)
      if (status === 'failed') {
        expect(screen.queryByText('Provider failed')).not.toBeInTheDocument()
        expect(
          screen.getByText(
            operationType === 'material_analysis'
              ? 'Material analysis did not complete. Check the material and provider settings, then retry.'
              : operationType === 'aster_planning'
                ? 'ASTER planning did not complete. Check the project inputs and provider settings, then retry.'
                : 'Rendering did not complete. Check the source materials and render settings, then retry.',
          ),
        ).toHaveAttribute('role', 'alert')
      }
    },
  )

  it('shows a safe localised Problem when stopping an attempt fails', async () => {
    const runningItem = {
      attempt: { ...attempt, status: 'running' },
      job: { ...job, status: 'running' },
      navigation: {
        type: 'run',
        project_id: 'project_1',
        project_name: 'Trailer',
        run_id: 'run_1',
        run_sequence: 2,
      },
    }
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) =>
        (init?.method ?? 'GET') === 'POST'
          ? problemResponse(409, 'The worker has already completed this attempt.')
          : jsonResponse({ items: [runningItem] }),
      ),
    )
    render(
      <QueryClientProvider client={queryClient()}>
        <MemoryRouter>
          <ActivityBoard />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    await userEvent.click(await screen.findByRole('button', { name: 'Stop Attempt' }))

    expect(
      await screen.findByText(
        'The operation could not be completed. Check the current state and try again.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByRole('alert')).toBeInTheDocument()
  })
})

describe('Activity management', () => {
  it('selects individual Recent entries and clears only the selected history', async () => {
    let items = [
      {
        attempt: { ...attempt, attempt_id: 'attempt_1', sequence: 1 },
        job,
        navigation: null,
      },
      {
        attempt: { ...attempt, attempt_id: 'attempt_2', sequence: 2 },
        job: { ...job, attempt_id: 'attempt_2' },
        navigation: null,
      },
    ]
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = String(input)
      if (path === '/api/activity/dismiss' && init?.method === 'POST') {
        const payload = JSON.parse(String(init.body)) as { attempt_ids: string[] }
        items = items.filter(
          (item) => !payload.attempt_ids.includes(item.attempt.attempt_id),
        )
        return jsonResponse({
          attempt_ids: payload.attempt_ids,
          dismissed: payload.attempt_ids.length,
        })
      }
      if (path === '/api/activity') {
        return jsonResponse({ items, limit: 100, offset: 0, has_more: false })
      }
      throw new Error(`Unexpected request: ${path}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    render(
      <QueryClientProvider client={queryClient()}>
        <MemoryRouter>
          <ActivityBoard />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    const recent = (await screen.findByText('Recent')).closest('section')
    expect(recent).not.toBeNull()
    await userEvent.click(within(recent!).getByRole('button', { name: 'Manage' }))
    const checkboxes = within(recent!).getAllByRole('checkbox')
    await userEvent.click(checkboxes[0])
    await userEvent.click(within(recent!).getByRole('button', { name: 'Clear (1)' }))

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/activity/dismiss',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ attempt_ids: ['attempt_1'] }),
      }),
    )
    expect(await screen.findByText('#2')).toBeInTheDocument()
    expect(screen.queryByText('#1')).not.toBeInTheDocument()
  })

  it('supports selecting and deselecting every entry in one managed group', async () => {
    const items = [
      {
        attempt: { ...attempt, attempt_id: 'attempt_1', sequence: 1 },
        job,
        navigation: null,
      },
      {
        attempt: { ...attempt, attempt_id: 'attempt_2', sequence: 2 },
        job: { ...job, attempt_id: 'attempt_2' },
        navigation: null,
      },
    ]
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({ items, limit: 100, offset: 0, has_more: false }),
      ),
    )
    render(
      <QueryClientProvider client={queryClient()}>
        <MemoryRouter>
          <ActivityBoard />
        </MemoryRouter>
      </QueryClientProvider>,
    )

    const recent = (await screen.findByText('Recent')).closest('section')
    expect(recent).not.toBeNull()
    await userEvent.click(within(recent!).getByRole('button', { name: 'Manage' }))
    await userEvent.click(within(recent!).getByRole('button', { name: 'Select all' }))

    expect(
      within(recent!)
        .getAllByRole<HTMLInputElement>('checkbox')
        .every((item) => item.checked),
    ).toBe(true)
    expect(within(recent!).getByRole('button', { name: 'Clear (2)' })).toBeEnabled()

    await userEvent.click(
      within(recent!).getByRole('button', { name: 'Clear selection' }),
    )
    expect(
      within(recent!)
        .getAllByRole<HTMLInputElement>('checkbox')
        .every((item) => !item.checked),
    ).toBe(true)
  })
})
