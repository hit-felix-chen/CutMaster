import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { MaterialsWorkspace } from '@/features/materials/MaterialsWorkspace'
import { MaterialsPage } from '@/pages/MaterialsPage'
import '@/i18n'

const material = {
  material_id: 'mat_video',
  material_type: 'video',
  name: 'La La Land',
  condition: 'ready',
  reused: true,
  duration_sec: 120,
  analysis_available: true,
  reference_count: 0,
  references: [],
}

function jsonResponse(value: unknown) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

afterEach(() => vi.unstubAllGlobals())

describe('Material Memory deep links', () => {
  it('switches the production Material route from video to music', async () => {
    const music = {
      ...material,
      material_id: 'mat_music',
      material_type: 'music',
      name: 'Main Score',
    }
    const requests: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string | URL | Request) => {
        const url = String(input)
        requests.push(url)
        if (url.includes('type=music')) {
          return Promise.resolve(jsonResponse({ items: [music], total: 1 }))
        }
        if (url.includes('type=video')) {
          return Promise.resolve(jsonResponse({ items: [material], total: 1 }))
        }
        return Promise.resolve(new Response(null, { status: 404 }))
      }),
    )
    const router = createMemoryRouter(
      [
        { path: '/materials/:type', element: <MaterialsPage /> },
        { path: '/materials/:type/:materialId', element: <MaterialsPage /> },
        {
          path: '/materials/:type/:materialId/memory/:tab',
          element: <MaterialsPage />,
        },
      ],
      { initialEntries: ['/materials/video'] },
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('La La Land')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('link', { name: 'Music' }))

    expect(await screen.findByText('Main Score')).toBeInTheDocument()
    expect(screen.queryByText('La La Land')).not.toBeInTheDocument()
    expect(router.state.location.pathname).toBe('/materials/music')
    expect(requests.some((url) => url.includes('type=music'))).toBe(true)
    expect(screen.getByRole('link', { name: /Main Score/ })).toHaveAttribute(
      'href',
      '/materials/music/mat_music',
    )
  })

  it('loads the dedicated tab and preserves parent list query state', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string | URL | Request) => {
        const url = String(input)
        if (url.includes('/memory/story')) {
          return Promise.resolve(
            jsonResponse({
              material_id: material.material_id,
              material_type: 'video',
              tab: 'story',
              limit: 100,
              offset: 0,
              payload: {
                title: 'A real story',
                logline: 'An artist keeps moving forward.',
                synopsis: 'The analysis-backed synopsis.',
                chronological_story_beats: [],
                character_arcs: [],
                themes: ['ambition'],
              },
            }),
          )
        }
        if (url === '/api/materials/mat_video')
          return Promise.resolve(jsonResponse(material))
        if (url.startsWith('/api/materials?')) {
          return Promise.resolve(jsonResponse({ items: [material], total: 1 }))
        }
        return Promise.resolve(new Response(null, { status: 404 }))
      }),
    )

    const router = createMemoryRouter(
      [
        {
          path: '/materials/:type/:materialId/memory/:tab',
          element: <MaterialsWorkspace />,
        },
      ],
      {
        initialEntries: [
          '/materials/video/mat_video/memory/story?search=La&sort=name_desc',
        ],
      },
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('A real story')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Timeline' })).toHaveAttribute(
      'href',
      '/materials/video/mat_video/memory/timeline?search=La&sort=name_desc',
    )
  })

  it('closes Memory before its Drawer without reopening either on Back', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string | URL | Request) => {
        const url = String(input)
        if (url.includes('/memory/story')) {
          return Promise.resolve(
            jsonResponse({
              material_id: material.material_id,
              material_type: 'video',
              tab: 'story',
              payload: { title: 'A real story' },
            }),
          )
        }
        if (url === '/api/materials/mat_video') {
          return Promise.resolve(jsonResponse(material))
        }
        if (url.startsWith('/api/materials?')) {
          return Promise.resolve(jsonResponse({ items: [material], total: 1 }))
        }
        return Promise.resolve(new Response(null, { status: 404 }))
      }),
    )

    const listPath = '/materials/video?search=La'
    const detailPath = '/materials/video/mat_video?search=La'
    const router = createMemoryRouter(
      [
        { path: '/materials/:type', element: <MaterialsWorkspace /> },
        {
          path: '/materials/:type/:materialId',
          element: <MaterialsWorkspace />,
        },
        {
          path: '/materials/:type/:materialId/memory/:tab',
          element: <MaterialsWorkspace />,
        },
      ],
      {
        initialEntries: [
          listPath,
          {
            pathname: '/materials/video/mat_video',
            search: '?search=La',
            state: { overlayParent: listPath },
          },
          {
            pathname: '/materials/video/mat_video/memory/story',
            search: '?search=La',
            state: { overlayParent: detailPath },
          },
        ],
        initialIndex: 2,
      },
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('A real story')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('link', { name: 'Timeline' }))
    await waitFor(() =>
      expect(router.state.location.pathname).toBe(
        '/materials/video/mat_video/memory/timeline',
      ),
    )
    const closeButtons = screen.getAllByRole('button', { name: 'Close' })
    fireEvent.click(closeButtons.at(-1)!)
    await waitFor(() =>
      expect(router.state.location.pathname).toBe('/materials/video/mat_video'),
    )

    fireEvent.click(screen.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(router.state.location.pathname).toBe('/materials/video'))
  })

  it('canonicalizes an invalid Memory tab without losing the Material route', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string | URL | Request) => {
        const url = String(input)
        if (url.includes('/memory/timeline')) {
          return Promise.resolve(
            jsonResponse({
              material_id: material.material_id,
              material_type: 'video',
              tab: 'timeline',
              payload: { segments: { items: [], total: 0 }, source: {} },
            }),
          )
        }
        if (url === '/api/materials/mat_video') {
          return Promise.resolve(jsonResponse(material))
        }
        if (url.startsWith('/api/materials?')) {
          return Promise.resolve(jsonResponse({ items: [material], total: 1 }))
        }
        return Promise.resolve(new Response(null, { status: 404 }))
      }),
    )
    const router = createMemoryRouter(
      [
        {
          path: '/materials/:type/:materialId/memory/:tab',
          element: <MaterialsWorkspace />,
        },
      ],
      { initialEntries: ['/materials/video/mat_video/memory/not-real'] },
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })

    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    await waitFor(() =>
      expect(router.state.location.pathname).toBe(
        '/materials/video/mat_video/memory/timeline',
      ),
    )
  })
})
