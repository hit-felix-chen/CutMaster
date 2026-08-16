import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { DataRootMigrationPanel } from '@/features/settings/DataRootMigrationPanel'
import i18n from '@/i18n'

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      'Content-Type': status >= 400 ? 'application/problem+json' : 'application/json',
    },
  })
}

function migration(
  status = 'copying',
  progress = {
    phase: 'copying',
    files_completed: 3,
    files_total: 10,
    bytes_completed: 512,
    bytes_total: 0,
  },
) {
  return {
    migration_id: 'drm_1',
    source_root: '/workspace/.cutmaster',
    destination_root: '/Volumes/Media/CutMaster',
    status,
    progress,
    cancel_requested: false,
    blockers: [],
    failure: null,
    created_at: '2026-08-16T00:00:00Z',
    updated_at: '2026-08-16T00:00:01Z',
    started_at: '2026-08-16T00:00:01Z',
    finished_at: null,
  }
}

function renderPanel() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <DataRootMigrationPanel />
    </QueryClientProvider>,
  )
  return client
}

beforeEach(async () => {
  await i18n.changeLanguage('en-US')
})

afterEach(() => vi.unstubAllGlobals())

describe('Data Root Migration', () => {
  it('requires an absolute path, preflights real estimates, and starts on two clicks', async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        requests.push({ url, init })
        if (url === '/api/settings/storage/migrations/current') {
          return jsonResponse({ migration: null })
        }
        if (url === '/api/settings/storage/migrations/preflight') {
          return jsonResponse({
            source_root: '/workspace/.cutmaster',
            destination_root: '/Volumes/Media/CutMaster',
            eligible: true,
            estimated_file_count: 1234,
            estimated_size_bytes: 2 * 1024 * 1024,
            blockers: [],
          })
        }
        if (url === '/api/settings/storage/migrations') {
          return jsonResponse(
            migration('requested', {
              phase: 'requested',
              files_completed: 0,
              files_total: 0,
              bytes_completed: 0,
              bytes_total: 0,
            }),
            202,
          )
        }
        return new Response(null, { status: 404 })
      }),
    )
    const user = userEvent.setup()
    renderPanel()

    const input = await screen.findByLabelText('New absolute directory path')
    await user.type(input, 'relative/path')
    await user.click(screen.getByRole('button', { name: 'Check destination' }))
    expect(await screen.findByText('Enter an absolute directory path.')).toBeVisible()
    expect(requests.some(({ url }) => url.endsWith('/migrations/preflight'))).toBe(
      false,
    )

    await user.clear(input)
    await user.type(input, '/Volumes/Media/CutMaster')
    await user.click(screen.getByRole('button', { name: 'Check destination' }))

    expect(await screen.findByText('1,234')).toBeVisible()
    expect(screen.getByText('2 MB')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Start migration' }))
    expect(
      requests.some(
        ({ url, init }) => url.endsWith('/migrations') && init?.method === 'POST',
      ),
    ).toBe(false)

    await user.click(screen.getByRole('button', { name: 'Click again to start' }))
    await waitFor(() =>
      expect(
        requests.some(
          ({ url, init }) =>
            url.endsWith('/migrations') &&
            init?.method === 'POST' &&
            new Headers(init.headers).has('Idempotency-Key') &&
            init.body ===
              JSON.stringify({ destination_root: '/Volumes/Media/CutMaster' }),
        ),
      ).toBe(true),
    )
    expect(await screen.findByText('Requested')).toBeVisible()
    expect(screen.queryByRole('progressbar')).toBeNull()
    expect(
      screen.getAllByText('The authoritative total is not available yet.'),
    ).toHaveLength(2)
  })

  it('shows measured and unknown totals honestly and sends a real cancel command', async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        requests.push({ url, init })
        if (url === '/api/settings/storage/migrations/current') {
          return jsonResponse({ migration: migration() })
        }
        if (url.endsWith('/drm_1/cancel')) {
          return jsonResponse({
            ...migration('cancelling', {
              phase: 'cancelling',
              files_completed: 3,
              files_total: 10,
              bytes_completed: 512,
              bytes_total: 0,
            }),
            cancel_requested: true,
          })
        }
        return new Response(null, { status: 404 })
      }),
    )
    const user = userEvent.setup()
    renderPanel()

    const fileProgress = await screen.findByRole('progressbar', {
      name: 'Files copied',
    })
    expect(fileProgress).toHaveAttribute('aria-valuenow', '3')
    expect(fileProgress).toHaveAttribute('aria-valuemax', '10')
    expect(screen.queryByRole('progressbar', { name: 'Data copied' })).toBeNull()
    expect(screen.getByText('512 B copied')).toBeVisible()

    await user.click(screen.getByRole('button', { name: 'Cancel migration' }))
    await waitFor(() =>
      expect(
        requests.some(
          ({ url, init }) =>
            url.endsWith('/drm_1/cancel') &&
            init?.method === 'POST' &&
            new Headers(init.headers).has('Idempotency-Key'),
        ),
      ).toBe(true),
    )
    expect(await screen.findByText('Cancelling')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Cancel migration' })).toBeNull()
  })

  it('localizes preflight and Problem Details blockers without exposing a start action', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        if (url === '/api/settings/storage/migrations/current') {
          return jsonResponse({ migration: null })
        }
        if (url === '/api/settings/storage/migrations/preflight') {
          return jsonResponse({
            source_root: '/workspace/.cutmaster',
            destination_root: '/Volumes/Media/CutMaster',
            eligible: false,
            estimated_file_count: 1,
            estimated_size_bytes: 10,
            blockers: [
              {
                kind: 'active_attempt',
                detail: 'raw backend detail',
                metadata: { attempt_id: 'attempt_7', status: 'running' },
              },
              {
                kind: 'future_private_blocker',
                detail: '/private/future backend detail',
                metadata: {},
              },
            ],
          })
        }
        if (url === '/api/settings/storage/migrations' && init?.method === 'POST') {
          return jsonResponse(
            {
              code: 'data_root_migration_blocked',
              blockers: [
                {
                  kind: 'destination_not_empty',
                  detail: 'raw detail',
                  metadata: {},
                },
              ],
            },
            409,
          )
        }
        return new Response(null, { status: 404 })
      }),
    )
    const user = userEvent.setup()
    renderPanel()

    await user.type(
      await screen.findByLabelText('New absolute directory path'),
      '/Volumes/Media/CutMaster',
    )
    await user.click(screen.getByRole('button', { name: 'Check destination' }))

    expect(
      await screen.findByText(
        'Attempt attempt_7 is still running. Stop or finish it first.',
      ),
    ).toBeVisible()
    expect(screen.queryByText('raw backend detail')).toBeNull()
    expect(
      screen.getByText('The current filesystem state blocks this migration.'),
    ).toBeVisible()
    expect(screen.queryByText(/private\/future/)).toBeNull()
    expect(screen.queryByRole('button', { name: 'Start migration' })).toBeNull()
  })

  it('localizes a stable failure code, hides raw text, and allows another preflight', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request) => {
        if (String(input) === '/api/settings/storage/migrations/current') {
          return jsonResponse({
            migration: {
              ...migration('failed'),
              failure: {
                code: 'migration_execution_failed',
                message: '/private/root and implementation details',
              },
            },
          })
        }
        return new Response(null, { status: 404 })
      }),
    )
    renderPanel()

    expect(
      await screen.findByText(
        'Copying or verification failed. The active Data Root was not switched.',
      ),
    ).toBeVisible()
    expect(screen.queryByText(/private\/root/)).toBeNull()
    expect(screen.getByLabelText('New absolute directory path')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Check destination' })).toBeVisible()
  })

  it.each(['switching', 'restart_required'])(
    'does not offer cancellation once migration is %s',
    async (status) => {
      vi.stubGlobal(
        'fetch',
        vi.fn(async (input: string | URL | Request) => {
          if (String(input) === '/api/settings/storage/migrations/current') {
            return jsonResponse({ migration: migration(status) })
          }
          return new Response(null, { status: 404 })
        }),
      )
      renderPanel()

      expect(await screen.findByText('Data Root Migration')).toBeVisible()
      expect(screen.queryByRole('button', { name: 'Cancel migration' })).toBeNull()
    },
  )

  it('allows a new preflight after cancellation', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request) => {
        if (String(input) === '/api/settings/storage/migrations/current') {
          return jsonResponse({ migration: migration('cancelled') })
        }
        return new Response(null, { status: 404 })
      }),
    )
    renderPanel()

    expect(await screen.findByLabelText('New absolute directory path')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Check destination' })).toBeVisible()
  })
})
