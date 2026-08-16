import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { createMemoryRouter, Outlet, RouterProvider } from 'react-router-dom'
import { describe, expect, it, vi } from 'vitest'

import { CreativeBrief } from '@/features/projects/ProjectWorkspace'
import type { ProjectWorkspace } from '@/features/shared/api'
import '@/i18n'

describe('Creative Brief duration validation', () => {
  it('disables Save when target duration exceeds the selected music', () => {
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

    const duration = screen.getByLabelText(/Target Duration/)
    fireEvent.change(duration, {
      target: { value: '01:30' },
    })

    expect(duration).toHaveAttribute('aria-invalid', 'true')
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled()
  })
})
