import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ProjectOutputsView } from '@/features/renders/ProjectOutputs'
import type {
  ExecutionSummary,
  RenderVariant,
  RenderVariantListItem,
} from '@/features/shared/api'
import i18n from '@/i18n'

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function execution(status: string): ExecutionSummary {
  return {
    attempt: {
      attempt_id: 'attempt_render',
      operation_type: 'rendering',
      owner_type: 'render_variant',
      owner_id: 'render_failed',
      sequence: 2,
      status,
      created_at: '2026-08-16T00:00:00Z',
      updated_at: '2026-08-16T00:00:00Z',
    },
    job: {
      job_id: 'job_render',
      attempt_id: 'attempt_render',
      status,
      stop_requested: false,
      progress: {},
      created_at: '2026-08-16T00:00:00Z',
      updated_at: '2026-08-16T00:00:00Z',
    },
  }
}

function variant(
  id: string,
  status: RenderVariant['status'],
  mode: RenderVariant['specification']['audio_mode'],
): RenderVariant {
  return {
    render_variant_id: id,
    project_id: 'project_1',
    run_id: 'run_3',
    run_sequence: 3,
    edit_id: 'edit_2',
    edit_sequence: 2,
    edit_origin: 'guided_revision',
    status,
    specification: {
      schema_version: '1.0',
      audio_mode: mode,
      renderer: {
        width: 1920,
        height: 1080,
        fps: 30,
        encoder: 'libx264',
        threads: 8,
        bgm_volume: 0.3,
        original_volume: 0,
        audio_sample_rate: 48000,
        dialogue_audio: {},
      },
    },
    frame_count: status === 'ready' ? 900 : null,
    duration_sec: status === 'ready' ? 30 : null,
    size_bytes: status === 'ready' ? 123_456 : null,
    failure_message: status === 'failed' ? 'Renderer exited with code 1' : null,
    media_url: status === 'ready' ? `/api/render-variants/${id}/media` : null,
    download_url: status === 'ready' ? `/api/render-variants/${id}/download` : null,
    created_at: '2026-08-16T00:00:00Z',
    updated_at: '2026-08-16T00:00:00Z',
  }
}

function renderOutputs() {
  const router = createMemoryRouter(
    [
      {
        path: '/projects/:projectId/outputs',
        element: <ProjectOutputsView />,
      },
      {
        path: '/projects/:projectId/runs/:runId/review/:editId',
        element: <h1>Review target</h1>,
      },
    ],
    { initialEntries: ['/projects/project_1/outputs'] },
  )
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

beforeEach(async () => {
  await i18n.changeLanguage('en-US')
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('Project Outputs', () => {
  it('lists the real project projection and supports Play, Download and Retry', async () => {
    let items: RenderVariantListItem[] = [
      { render_variant: variant('render_ready', 'ready', 'dialogue'), execution: null },
      {
        render_variant: variant('render_failed', 'failed', 'bgm_only'),
        execution: null,
      },
    ]
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/projects/project_1/render-variants')) {
        return jsonResponse({ items, count: items.length })
      }
      if (
        url.endsWith('/api/render-variants/render_failed/retry') &&
        init?.method === 'POST'
      ) {
        const queued = variant('render_failed', 'queued', 'bgm_only')
        items = [
          { render_variant: items[0].render_variant, execution: null },
          {
            render_variant: queued,
            execution: execution('queued'),
          },
        ]
        return jsonResponse(
          { render_variant: queued, execution: execution('queued'), created: false },
          202,
        )
      }
      return new Response(null, { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const router = renderOutputs()
    const user = userEvent.setup()

    expect(await screen.findByText('Dialogue Preview')).toBeVisible()
    expect(screen.getByText('BGM-only')).toBeVisible()
    const readyCard = screen.getByText('Dialogue Preview').closest('article')
    const failedCard = screen.getByText('BGM-only').closest('article')
    expect(readyCard).not.toBeNull()
    expect(failedCard).not.toBeNull()
    expect(
      within(failedCard as HTMLElement).getByText(
        'Rendering did not complete. Check the source materials and render settings, then retry.',
      ),
    ).toHaveAttribute('role', 'alert')
    expect(
      within(failedCard as HTMLElement).queryByText('Renderer exited with code 1'),
    ).not.toBeInTheDocument()
    await user.click(
      within(readyCard as HTMLElement).getByRole('button', { name: 'Play' }),
    )
    expect(router.state.location.search).toBe('?variant=render_ready')
    expect(
      await waitFor(() => document.querySelector('.output-player video')),
    ).toHaveAttribute('src', '/api/render-variants/render_ready/media')
    expect(
      within(readyCard as HTMLElement).getByRole('link', { name: 'Download copy' }),
    ).toHaveAttribute('href', '/api/render-variants/render_ready/download')

    await user.click(
      within(failedCard as HTMLElement).getByRole('button', { name: 'Retry' }),
    )
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/render-variants/render_failed/retry',
        expect.objectContaining({ method: 'POST' }),
      ),
    )
    expect(await screen.findByText('Queued')).toBeVisible()
  })

  it('keeps the Outputs route while deleting a selected Variant on the second click', async () => {
    let items: RenderVariantListItem[] = [
      { render_variant: variant('render_ready', 'ready', 'dialogue'), execution: null },
    ]
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
        const url = String(input)
        if (url.endsWith('/api/projects/project_1/render-variants')) {
          return jsonResponse({ items, count: items.length })
        }
        if (
          url.endsWith('/api/render-variants/render_ready') &&
          init?.method === 'DELETE'
        ) {
          items = []
          return jsonResponse({ render_variant_id: 'render_ready', deleted: true })
        }
        return new Response(null, { status: 404 })
      }),
    )
    const router = renderOutputs()
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Play' }))
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(screen.getByRole('button', { name: 'Click again to delete' }))

    expect(await screen.findByText('No outputs yet')).toBeVisible()
    expect(router.state.location.pathname).toBe('/projects/project_1/outputs')
    expect(router.state.location.search).toBe('')
  })
})
