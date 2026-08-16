import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { AppShell } from '@/components/layout/AppShell'
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

afterEach(() => vi.unstubAllGlobals())

describe('AppShell Data Root gate', () => {
  it('persistently blocks business navigation and actions after the pointer switch', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input)
        if (url === '/api/health') {
          return jsonResponse({
            status: 'restart_required',
            service: 'cutmaster',
            data_root: {
              maintenance: true,
              restart_required: true,
              migration_id: 'drm_1',
              migration_status: 'restart_required',
            },
            configured: { llm: true, vlm: true, asr: true },
          })
        }
        if (url === '/api/settings/storage/migrations/current') {
          return jsonResponse({
            migration: {
              migration_id: 'drm_1',
              source_root: '/old/.cutmaster',
              destination_root: '/new/.cutmaster',
              status: 'restart_required',
              progress: {
                phase: 'restart_required',
                files_completed: 10,
                files_total: 10,
                bytes_completed: 1024,
                bytes_total: 1024,
              },
              cancel_requested: false,
              blockers: [],
              failure: null,
              created_at: '2026-08-16T00:00:00Z',
              updated_at: '2026-08-16T00:00:01Z',
              started_at: '2026-08-16T00:00:00Z',
              finished_at: null,
            },
          })
        }
        return new Response(null, { status: 404 })
      }),
    )
    const router = createMemoryRouter(
      [
        {
          path: '/',
          element: <AppShell />,
          children: [
            {
              path: 'projects',
              element: <button type="button">Dangerous project action</button>,
            },
            { path: 'settings', element: <p>Migration settings content</p> },
          ],
        },
      ],
      { initialEntries: ['/projects'] },
    )
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(
      await screen.findByText(
        'The Data Root switch is committed. Restart CutMaster; the old directory remains untouched.',
      ),
    ).toBeVisible()
    expect(
      screen.getByRole('heading', { name: 'Restart CutMaster to finish' }),
    ).toBeVisible()
    expect(
      screen.queryByRole('button', { name: 'Dangerous project action' }),
    ).toBeNull()
    expect(screen.queryByRole('link', { name: 'Projects' })).toBeNull()
    expect(screen.getByText('Projects').closest('[aria-disabled]')).toHaveAttribute(
      'aria-disabled',
      'true',
    )
    expect(screen.getByRole('link', { name: 'Settings' })).toBeVisible()

    await userEvent.click(screen.getAllByRole('link', { name: 'View migration' })[0])
    expect(await screen.findByText('Migration settings content')).toBeVisible()
    expect(router.state.location.pathname).toBe('/settings')
    expect(
      screen.getByText(
        'The Data Root switch is committed. Restart CutMaster; the old directory remains untouched.',
      ),
    ).toBeVisible()
  })
})
