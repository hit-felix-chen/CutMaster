import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { ApplicationGate } from '@/app/ApplicationGate'
import { SetupPage } from '@/pages/SetupPage'
import '@/i18n'

function healthResponse(llm: boolean, vlm: boolean, asr: boolean) {
  return new Response(
    JSON.stringify({
      status: 'ok',
      service: 'cutmaster',
      configured: { llm, vlm, asr },
    }),
    { status: 200, headers: { 'Content-Type': 'application/json' } },
  )
}

afterEach(() => vi.unstubAllGlobals())

describe('Application health gate', () => {
  it('redirects missing model connections to the real Setup state', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string | URL | Request) => {
        if (String(input) === '/api/health') {
          return Promise.resolve(healthResponse(true, false, false))
        }
        return Promise.resolve(
          new Response(
            JSON.stringify({
              title: 'Settings unavailable',
              detail: 'Provider Settings could not be loaded.',
              code: 'settings_unavailable',
            }),
            {
              status: 503,
              headers: { 'Content-Type': 'application/problem+json' },
            },
          ),
        )
      }),
    )
    const router = createMemoryRouter(
      [
        { path: '/', element: <ApplicationGate /> },
        { path: '/setup', element: <SetupPage /> },
      ],
      { initialEntries: ['/'] },
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByRole('heading', { name: 'Setup required' })).toBeVisible()
    expect(screen.getByText('Missing connections: VLM, ASR')).toBeVisible()
    expect(router.state.location.pathname).toBe('/setup')
  })

  it('opens the application shell when every connection is configured', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(() => Promise.resolve(healthResponse(true, true, true))),
    )
    const router = createMemoryRouter(
      [
        {
          path: '/',
          element: <ApplicationGate />,
          children: [{ index: true, element: <p>Workspace ready</p> }],
        },
      ],
      { initialEntries: ['/'] },
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('Workspace ready')).toBeVisible()
  })
})
