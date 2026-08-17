import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { createMemoryRouter, Outlet, RouterProvider } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { CreativeBrief } from '@/features/projects/ProjectWorkspace'
import type { ProjectWorkspace } from '@/features/shared/api'
import '@/i18n'

describe('Creative Brief duration validation', () => {
  it('constrains custom minutes and seconds to the selected music duration', () => {
    const workspace: ProjectWorkspace = {
      project: {
        project_id: 'project_1',
        name: 'Project',
        video_material_ids: [],
        music_material_ids: ['mat_music'],
        creative_brief: {
          editing_intent: 'Preserve the emotional arc.',
          target_duration_sec: 30,
        },
        created_at: '2026-08-12T00:00:00Z',
        updated_at: '2026-08-12T00:00:00Z',
      },
      materials: {
        video: [],
        music: [
          {
            material_id: 'mat_music',
            material_type: 'music',
            name: 'Score',
            condition: 'ready',
            duration_sec: 60,
          },
        ],
      },
      runs: [],
    }
    const router = createMemoryRouter([
      {
        path: '/',
        element: <Outlet context={{ workspace, refetch: vi.fn() }} />,
        children: [{ index: true, element: <CreativeBrief /> }],
      },
    ])
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(screen.getByRole('radio', { name: 'Custom duration' })).toBeChecked()

    const minutes = screen.getByRole('spinbutton', { name: 'Minutes' })
    const seconds = screen.getByRole('spinbutton', { name: 'Seconds' })
    expect(minutes).toHaveValue(0)
    expect(minutes).toHaveAttribute('max', '1')
    expect(seconds).toHaveValue(30)

    fireEvent.change(minutes, {
      target: { value: '1' },
    })

    expect(minutes).toHaveValue(1)
    expect(seconds).toHaveValue(0)
    expect(seconds).toHaveAttribute('max', '0')

    fireEvent.change(seconds, { target: { value: '45' } })
    expect(seconds).toHaveValue(0)
  })

  it('uses the exact selected music duration when that mode is selected', async () => {
    const workspace: ProjectWorkspace = {
      project: {
        project_id: 'project_1',
        name: 'Project',
        video_material_ids: [],
        music_material_ids: ['mat_music'],
        creative_brief: {
          editing_intent: 'Preserve the emotional arc.',
          target_duration_sec: 30,
        },
        created_at: '2026-08-12T00:00:00Z',
        updated_at: '2026-08-12T00:00:00Z',
      },
      materials: {
        video: [],
        music: [
          {
            material_id: 'mat_music',
            material_type: 'music',
            name: 'Score',
            condition: 'ready',
            duration_sec: 60.5,
          },
        ],
      },
      runs: [],
    }
    const requests: RequestInit[] = []
    const fetchMock = vi.fn(
      async (_input: string | URL | Request, init?: RequestInit) => {
        requests.push(init ?? {})
        return new Response(JSON.stringify(workspace.project), {
          headers: { 'Content-Type': 'application/json' },
        })
      },
    )
    vi.stubGlobal('fetch', fetchMock)
    const router = createMemoryRouter([
      {
        path: '/',
        element: <Outlet context={{ workspace, refetch: vi.fn() }} />,
        children: [{ index: true, element: <CreativeBrief /> }],
      },
    ])
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    fireEvent.click(screen.getByRole('radio', { name: 'Use audio duration' }))
    expect(
      screen.queryByRole('spinbutton', { name: 'Minutes' }),
    ).not.toBeInTheDocument()
    expect(screen.getByText('01:01')).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }))

    await vi.waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1))
    expect(JSON.parse(String(requests[0]?.body))).toMatchObject({
      target_duration_sec: 60.5,
    })
  })
})
