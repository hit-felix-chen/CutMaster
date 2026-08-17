import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ProjectLayout, ProjectOverview } from '@/features/projects/ProjectWorkspace'
import '@/i18n'

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => vi.unstubAllGlobals())

describe('consolidated Project setup', () => {
  it('saves one setup snapshot, guards dirty Start, then creates a real Run', async () => {
    const video = {
      material_id: 'mat_video',
      material_type: 'video',
      name: 'Feature Film',
      condition: 'ready',
      duration_sec: 120,
    }
    const music = {
      material_id: 'mat_music',
      material_type: 'music',
      name: 'Main Score',
      condition: 'ready',
      duration_sec: 90,
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
    let workspace = {
      project,
      materials: { video: [video], music: [music] },
      runs: [],
    }
    const requests: Array<{ url: string; method: string }> = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        const method = init?.method ?? 'GET'
        requests.push({ url, method })
        if (url.endsWith('/workspace')) return jsonResponse(workspace)
        if (url.includes('/api/materials?type=video')) {
          return jsonResponse({ items: [video], total: 1 })
        }
        if (url.includes('/api/materials?type=music')) {
          return jsonResponse({ items: [music], total: 1 })
        }
        if (url.endsWith('/setup') && method === 'PUT') {
          const body = JSON.parse(String(init?.body))
          workspace = {
            ...workspace,
            project: { ...project, creative_brief: body },
          }
          return jsonResponse(workspace.project)
        }
        if (url.endsWith('/runs') && method === 'POST') {
          return jsonResponse(
            {
              run: {
                run_id: 'run_1',
                project_id: 'project_1',
                sequence: 1,
                status: 'queued',
                creative_brief: workspace.project.creative_brief,
                video_material_ids: ['mat_video'],
                music_material_ids: ['mat_music'],
                created_at: '2026-08-13T00:00:00Z',
                updated_at: '2026-08-13T00:00:00Z',
              },
              attempt: {},
              job: {},
            },
            202,
          )
        }
        return new Response(null, { status: 404 })
      }),
    )
    const router = createMemoryRouter(
      [
        {
          path: '/projects/:projectId',
          element: <ProjectLayout />,
          children: [
            { path: 'overview', element: <ProjectOverview /> },
            { path: 'runs/:runId', element: <p>Run opened</p> },
          ],
        },
      ],
      { initialEntries: ['/projects/project_1/overview'] },
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByRole('heading', { name: 'Project Setup' })).toBeVisible()
    expect(screen.getByRole('link', { name: 'Project Setup' })).toBeVisible()
    expect(screen.queryByRole('link', { name: 'Materials' })).not.toBeInTheDocument()
    expect(
      screen.queryByRole('link', { name: 'Creative Brief' }),
    ).not.toBeInTheDocument()
    expect(await screen.findByRole('radio', { name: 'Custom duration' })).toBeChecked()
    expect(screen.getByRole('spinbutton', { name: 'Minutes' })).toHaveValue(1)
    expect(screen.getByRole('spinbutton', { name: 'Seconds' })).toHaveValue(0)
    expect(screen.getByRole('spinbutton', { name: 'Seconds' })).toHaveAttribute(
      'max',
      '30',
    )

    fireEvent.change(screen.getByRole('textbox', { name: /Editing Intent/ }), {
      target: { value: 'Follow the emotional reunion.' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Start editing' }))
    expect(await screen.findByText('Project setup is not saved')).toBeVisible()
    expect(requests.filter((item) => item.url.endsWith('/runs'))).toHaveLength(0)

    fireEvent.click(screen.getByRole('button', { name: 'Back' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save project setup' }))
    await waitFor(() =>
      expect(requests.some((item) => item.url.endsWith('/setup'))).toBe(true),
    )
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Start editing' })).toBeEnabled(),
    )
    fireEvent.click(screen.getByRole('button', { name: 'Start editing' }))

    expect(await screen.findByText('Run opened')).toBeVisible()
    expect(requests.filter((item) => item.url.endsWith('/runs'))).toHaveLength(1)
    expect(router.state.location.pathname).toBe('/projects/project_1/runs/run_1')
  })
})
