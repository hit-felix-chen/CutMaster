import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ColorModeProvider } from '@/app/providers/ColorModeProvider'
import { LocaleProvider } from '@/app/providers/LocaleProvider'
import { SettingsWorkspace } from '@/features/settings/SettingsWorkspace'
import i18n from '@/i18n'

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function storage(revealSupported: boolean) {
  return {
    data_root: '/workspace/.cutmaster',
    categories: [
      { name: 'database', file_count: 1, size_bytes: 2048 },
      { name: 'materials', file_count: 0, size_bytes: 0 },
      { name: 'projects', file_count: 0, size_bytes: 0 },
      { name: 'direct', file_count: 0, size_bytes: 0 },
      { name: 'logs', file_count: 0, size_bytes: 0 },
    ],
    direct_bundle_count: 0,
    total_size_bytes: 2048,
    reveal_supported: revealSupported,
  }
}

function renderWorkspace() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <LocaleProvider>
        <ColorModeProvider>
          <SettingsWorkspace />
        </ColorModeProvider>
      </LocaleProvider>
    </QueryClientProvider>,
  )
}

beforeEach(async () => {
  await i18n.changeLanguage('en-US')
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({
      matches: false,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Settings storage actions', () => {
  it('shows and invokes the real Finder capability only when supported', async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        calls.push({ url, init })
        if (url === '/api/health') {
          return jsonResponse({
            status: 'ok',
            service: 'cutmaster',
            data_root: {
              maintenance: false,
              restart_required: false,
              migration_id: null,
              migration_status: null,
            },
            configured: { llm: true, vlm: true, asr: true },
          })
        }
        if (url === '/api/settings/storage/migrations/current') {
          return jsonResponse({ migration: null })
        }
        if (url === '/api/settings/storage') return jsonResponse(storage(true))
        if (url === '/api/settings/storage/reveal') {
          return jsonResponse({ opened: true })
        }
        return jsonResponse({ detail: 'not needed for this test' }, 404)
      }),
    )
    renderWorkspace()

    await userEvent.click(await screen.findByRole('button', { name: 'Open in Finder' }))

    await waitFor(() =>
      expect(
        calls.some(
          ({ url, init }) =>
            url === '/api/settings/storage/reveal' &&
            init?.method === 'POST' &&
            new Headers(init.headers).has('Idempotency-Key'),
        ),
      ).toBe(true),
    )
    expect(await screen.findByText(/opened in Finder/)).toBeVisible()
  })

  it('does not render a fake Finder action on an unsupported host', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input)
        if (url === '/api/health') {
          return jsonResponse({
            status: 'ok',
            service: 'cutmaster',
            data_root: {
              maintenance: false,
              restart_required: false,
              migration_id: null,
              migration_status: null,
            },
            configured: { llm: true, vlm: true, asr: true },
          })
        }
        if (url === '/api/settings/storage/migrations/current') {
          return jsonResponse({ migration: null })
        }
        if (url === '/api/settings/storage') {
          return jsonResponse(storage(false))
        }
        return jsonResponse({ detail: 'not needed for this test' }, 404)
      }),
    )
    renderWorkspace()

    expect(await screen.findByText('/workspace/.cutmaster')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Open in Finder' })).toBeNull()
  })
})
