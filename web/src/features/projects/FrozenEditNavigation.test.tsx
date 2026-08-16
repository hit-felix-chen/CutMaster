import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { appRoutes } from '@/app/routes'
import { ReviewUnavailable, RunDetail } from '@/features/projects/ProjectWorkspace'
import i18n from '@/i18n'

function jsonResponse(value: unknown) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeEach(async () => {
  await i18n.changeLanguage('en-US')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Frozen Edit navigation', () => {
  it('opens the exact Frozen Edit in the canonical Review route', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({
          run: {
            run_id: 'run_1',
            project_id: 'project_1',
            sequence: 1,
            status: 'complete',
            creative_brief: {
              editing_intent: 'Preserve the emotional arc.',
              target_duration_sec: 60,
            },
            video_material_ids: ['mat_video'],
            music_material_ids: ['mat_music'],
            failure_message: null,
            created_at: '2026-08-13T00:00:00Z',
            updated_at: '2026-08-13T00:01:00Z',
          },
          frozen_edits: [
            {
              edit_id: 'edit_1',
              run_id: 'run_1',
              sequence: 1,
              origin: 'initial',
              parent_edit_id: null,
              created_at: '2026-08-13T00:01:00Z',
            },
          ],
          execution: null,
        }),
      ),
    )
    const destination = appRoutes.review('project_1', 'run_1', 'edit_1')
    const router = createMemoryRouter(
      [
        {
          path: '/projects/:projectId/runs/:runId',
          element: <RunDetail />,
        },
        {
          path: '/projects/:projectId/runs/:runId/review/:editId',
          element: <ReviewUnavailable />,
        },
      ],
      { initialEntries: ['/projects/project_1/runs/run_1'] },
    )
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    const user = userEvent.setup()
    render(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    const editLink = await screen.findByRole('link', {
      name: /Frozen Edit #1/,
    })
    expect(editLink).toHaveAttribute('href', destination)

    await user.click(editLink)

    expect(router.state.location.pathname).toBe(destination)
    expect(await screen.findByRole('heading', { name: 'Unavailable' })).toBeVisible()
  })
})
