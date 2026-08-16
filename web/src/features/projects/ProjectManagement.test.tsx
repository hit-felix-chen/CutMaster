import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ProjectsLanding } from '@/features/projects/ProjectsLanding'
import i18n from '@/i18n'

const project = {
  project_id: 'project_1',
  name: 'Launch Film',
  video_material_ids: ['mat_video'],
  music_material_ids: ['mat_music'],
  creative_brief: {
    editing_intent: 'Preserve the emotional arc.',
    target_duration_sec: 60,
  },
  latest_run_state: 'running',
  preview_url: '/api/materials/mat_video/thumbnail',
  selected_materials: {
    video: [
      {
        material_id: 'mat_video',
        material_type: 'video',
        name: 'Feature Film',
        condition: 'ready',
      },
    ],
    music: [
      {
        material_id: 'mat_music',
        material_type: 'music',
        name: 'Main Score',
        condition: 'ready',
      },
    ],
  },
  created_at: '2026-08-13T00:00:00Z',
  updated_at: '2026-08-13T00:00:00Z',
}

function response(value: unknown, status = 200) {
  if (status === 204) return new Response(null, { status })
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function renderLanding() {
  const router = createMemoryRouter(
    [{ path: '/projects', element: <ProjectsLanding /> }],
    { initialEntries: ['/projects'] },
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

describe('Project landing management', () => {
  it('shows real card projections and performs rename and two-step delete', async () => {
    let currentProject: typeof project | null = project
    const requests: Array<{ url: string; init?: RequestInit }> = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        requests.push({ url, init })
        if (url === '/api/projects' && !init?.method) {
          return response({ items: currentProject ? [currentProject] : [] })
        }
        if (url === '/api/projects/project_1/rename' && init?.method === 'POST') {
          const body = JSON.parse(String(init.body)) as { name: string }
          currentProject = { ...project, name: body.name }
          return response(currentProject)
        }
        if (url === '/api/projects/project_1' && init?.method === 'DELETE') {
          currentProject = null
          return response(null, 204)
        }
        return response({ detail: 'Not found' }, 404)
      }),
    )
    renderLanding()

    expect(await screen.findByRole('heading', { name: 'Launch Film' })).toBeVisible()
    expect(screen.getByText(/Feature Film/)).toBeVisible()
    expect(screen.getByText(/Main Score/)).toBeVisible()
    expect(screen.getByText('Running')).toBeVisible()
    const preview = document.querySelector<HTMLImageElement>(
      '.project-card__preview img',
    )
    expect(preview).not.toBeNull()
    expect(preview).toHaveAttribute('src', '/api/materials/mat_video/thumbnail')
    fireEvent.error(preview!)
    expect(preview).not.toBeVisible()

    const user = userEvent.setup()
    await user.click(
      screen.getByRole('button', { name: 'Project actions for Launch Film' }),
    )
    await user.click(screen.getByRole('menuitem', { name: 'Rename' }))
    const renameDialog = screen.getByRole('dialog')
    const name = within(renameDialog).getByLabelText('Project name')
    await user.clear(name)
    await user.type(name, 'Launch Film v2')
    await user.click(within(renameDialog).getByRole('button', { name: 'Rename' }))

    expect(await screen.findByRole('heading', { name: 'Launch Film v2' })).toBeVisible()
    const renameRequest = requests.find(({ url }) => url.endsWith('/rename'))
    expect(renameRequest?.init?.method).toBe('POST')
    expect(new Headers(renameRequest?.init?.headers).has('Idempotency-Key')).toBe(true)

    await user.click(
      screen.getByRole('button', { name: 'Project actions for Launch Film v2' }),
    )
    await user.click(screen.getByRole('menuitem', { name: 'Delete' }))
    expect(requests.filter(({ init }) => init?.method === 'DELETE')).toHaveLength(0)
    const deleteDialog = screen.getByRole('alertdialog')
    await user.click(
      within(deleteDialog).getByRole('button', { name: 'Confirm delete' }),
    )

    await waitFor(() =>
      expect(requests.filter(({ init }) => init?.method === 'DELETE')).toHaveLength(1),
    )
    expect(await screen.findByText('Create your first edit project')).toBeVisible()
  })

  it('renders typed delete blockers returned by the backend', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        if (url === '/api/projects' && !init?.method) {
          return response({ items: [project] })
        }
        if (url === '/api/projects/project_1' && init?.method === 'DELETE') {
          return response(
            {
              title: 'Project has active attempts',
              detail: 'Stop the active attempt before deleting this project.',
              code: 'active_attempt_blocker',
              blockers: [
                {
                  type: 'attempt',
                  attempt_id: 'attempt_1',
                  run_id: 'run_1',
                },
              ],
            },
            409,
          )
        }
        return response({ detail: 'Not found' }, 404)
      }),
    )
    renderLanding()
    const user = userEvent.setup()

    await user.click(
      await screen.findByRole('button', {
        name: 'Project actions for Launch Film',
      }),
    )
    await user.click(screen.getByRole('menuitem', { name: 'Delete' }))
    await user.click(screen.getByRole('button', { name: 'Confirm delete' }))

    expect(
      await screen.findByText(
        'The operation could not be completed. Check the current state and try again.',
      ),
    ).toBeVisible()
    expect(screen.getByText('Attempt run_1 has not finished.')).toBeVisible()
  })
})
