import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ActivityBoard } from '@/features/activity/ActivityBoard'
import i18n from '@/i18n'

function jsonResponse(value: unknown) {
  return new Response(JSON.stringify(value), {
    status: 200,
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
      run_id: 'run_1',
    },
    pathname: '/projects/project_1/runs/run_1',
    search: '',
    accessibleOwner: 'Run',
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
    },
    pathname: '/materials/video/material_1',
    search: '',
    accessibleOwner: 'Material',
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
      run_id: 'run_1',
      edit_id: 'edit_1',
      render_variant_id: 'variant_1',
    },
    pathname: '/projects/project_1/runs/run_1/review/edit_1',
    search: '?variant=variant_1',
    accessibleOwner: 'Render Variant',
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
    async ({
      attempt: ownerAttempt,
      navigation,
      pathname,
      search,
      accessibleOwner,
    }) => {
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
        name: `Open ${accessibleOwner} ${ownerAttempt.owner_id}`,
      })
      await userEvent.click(link)

      expect(router.state.location.pathname).toBe(pathname)
      expect(router.state.location.search).toBe(search)
    },
  )
})
