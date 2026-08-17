import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
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
  it('shows the persisted analysis API cost in the drawer and Technical Memory', async () => {
    const costMaterial = { ...material, analysis_cost_yuan: 0.123456 }
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string | URL | Request) => {
        const url = String(input)
        if (url.includes('/memory/technical')) {
          return Promise.resolve(
            jsonResponse({
              material_id: material.material_id,
              material_type: 'video',
              tab: 'technical',
              payload: {
                schema_version: '3.0',
                source: { duration_sec: 120, fps: 24 },
                segment_count: 2,
              },
            }),
          )
        }
        if (url === '/api/materials/mat_video') {
          return Promise.resolve(jsonResponse(costMaterial))
        }
        if (url.startsWith('/api/materials?')) {
          return Promise.resolve(jsonResponse({ items: [costMaterial], total: 1 }))
        }
        return Promise.resolve(new Response(null, { status: 404 }))
      }),
    )
    const router = createMemoryRouter(
      [
        {
          path: '/materials/:type/:materialId?',
          element: <MaterialsWorkspace />,
        },
        {
          path: '/materials/:type/:materialId/memory/:tab',
          element: <MaterialsWorkspace />,
        },
      ],
      { initialEntries: ['/materials/video/mat_video'] },
    )
    render(
      <QueryClientProvider
        client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
      >
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('API cost')).toBeVisible()
    expect(screen.getByText(/0\.123456/)).toBeVisible()

    await act(() => router.navigate('/materials/video/mat_video/memory/technical'))
    const dialog = await screen.findByRole('dialog')
    expect(await within(dialog).findByText('API cost')).toBeVisible()
    expect(within(dialog).getByText(/0\.123456/)).toBeVisible()
  })

  it.each([
    {
      type: 'video',
      previewField: 'thumbnail_url',
      previewUrl: '/api/materials/mat_video/thumbnail',
    },
    {
      type: 'music',
      previewField: 'waveform_url',
      previewUrl: '/api/materials/mat_music/waveform',
    },
  ])(
    'uses only the lightweight $type card projection and falls back truthfully',
    async ({ type, previewField, previewUrl }) => {
      const requests: string[] = []
      const readyMaterial = {
        ...material,
        material_id: `mat_${type}`,
        material_type: type,
        [previewField]: previewUrl,
      }
      vi.stubGlobal(
        'fetch',
        vi.fn((input: string | URL | Request) => {
          const url = String(input)
          requests.push(url)
          if (url.startsWith('/api/materials?')) {
            return Promise.resolve(
              jsonResponse({
                items: [
                  readyMaterial,
                  {
                    ...readyMaterial,
                    material_id: `mat_${type}_queued`,
                    name: 'Waiting',
                    condition: 'queued',
                    [previewField]: null,
                  },
                  {
                    ...readyMaterial,
                    material_id: `mat_${type}_failed`,
                    name: 'Failed',
                    condition: 'failed',
                    [previewField]: null,
                  },
                ],
                total: 3,
              }),
            )
          }
          return Promise.resolve(new Response(null, { status: 404 }))
        }),
      )
      const router = createMemoryRouter(
        [
          {
            path: '/materials/:type/:materialId?',
            element: <MaterialsWorkspace />,
          },
        ],
        { initialEntries: [`/materials/${type}`] },
      )
      const client = new QueryClient({
        defaultOptions: { queries: { retry: false } },
      })
      render(
        <QueryClientProvider client={client}>
          <RouterProvider router={router} />
        </QueryClientProvider>,
      )

      await screen.findByText(readyMaterial.name)
      const image = document.querySelector<HTMLImageElement>('.material-preview__image')
      expect(image).not.toBeNull()
      expect(image).toHaveAttribute('src', previewUrl)
      expect(image).toHaveAttribute('loading', 'lazy')
      expect(image).toHaveAttribute('decoding', 'async')
      expect(document.querySelector('video, audio, source')).toBeNull()
      expect(requests.some((url) => url.includes('/source'))).toBe(false)
      expect(document.querySelector('[data-preview-state="queued"]')).not.toBeNull()
      expect(document.querySelector('[data-preview-state="failed"]')).not.toBeNull()

      fireEvent.error(image!)
      await waitFor(() =>
        expect(document.querySelector('[data-preview-state="ready"]')).not.toBeNull(),
      )
    },
  )

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
    expect(screen.getByRole('tab', { name: 'Timeline' })).toHaveAttribute(
      'href',
      '/materials/video/mat_video/memory/timeline?search=La&sort=name_desc',
    )
  })

  it('keeps Memory navigation in the header with accessible keyboard tabs', async () => {
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
              payload: {
                title: 'A real story',
                chronological_story_beats: [],
                character_arcs: [],
                themes: [],
              },
            }),
          )
        }
        if (url.includes('/memory/dialogue')) {
          return Promise.resolve(
            jsonResponse({
              material_id: material.material_id,
              material_type: 'video',
              tab: 'dialogue',
              payload: { sentences: { items: [], total: 0 } },
            }),
          )
        }
        if (url === '/api/materials/mat_video') {
          return Promise.resolve(jsonResponse(material))
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

    const dialog = await screen.findByRole('dialog')
    const header = dialog.querySelector('.memory-modal__header')
    const tablist = within(dialog).getByRole('tablist', {
      name: 'Material Memory Explorer',
    })
    expect(tablist.parentElement).toBe(header)

    const storyTab = within(tablist).getByRole('tab', { name: 'Story' })
    const timelineTab = within(tablist).getByRole('tab', { name: 'Timeline' })
    expect(storyTab).toHaveAttribute('aria-selected', 'true')
    expect(storyTab).toHaveAttribute('tabindex', '0')
    expect(timelineTab).toHaveAttribute('aria-selected', 'false')
    expect(timelineTab).toHaveAttribute('tabindex', '-1')
    expect(within(dialog).getByRole('tabpanel')).toHaveAttribute(
      'aria-labelledby',
      storyTab.id,
    )

    fireEvent.keyDown(storyTab, { key: 'ArrowRight' })
    await waitFor(() =>
      expect(router.state.location).toMatchObject({
        pathname: '/materials/video/mat_video/memory/dialogue',
        search: '?search=La&sort=name_desc',
      }),
    )
    await waitFor(() =>
      expect(within(tablist).getByRole('tab', { name: 'Dialogue' })).toHaveFocus(),
    )
  })

  it('uses the compact Structure and Technical tabs for music Memory', async () => {
    const music = {
      ...material,
      material_id: 'mat_music',
      material_type: 'music',
      name: 'Main Score',
    }
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string | URL | Request) => {
        const url = String(input)
        if (url.includes('/memory/structure')) {
          return Promise.resolve(
            jsonResponse({
              material_id: music.material_id,
              material_type: 'music',
              tab: 'structure',
              payload: {
                beats_sec: { items: [], total: 0 },
                accents_sec: { items: [], total: 0 },
                energy_curve: { items: [], total: 0 },
                sections: { items: [], total: 0 },
              },
            }),
          )
        }
        if (url === '/api/materials/mat_music') {
          return Promise.resolve(jsonResponse(music))
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
      { initialEntries: ['/materials/music/mat_music/memory/structure'] },
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    const dialog = await screen.findByRole('dialog')
    const tablist = within(dialog).getByRole('tablist', {
      name: 'Material Memory Explorer',
    })
    expect(tablist.parentElement).toBe(dialog.querySelector('.memory-modal__header'))
    expect(
      within(tablist)
        .getAllByRole('tab')
        .map((item) => item.textContent),
    ).toEqual(['Structure', 'Technical'])
    expect(within(tablist).getByRole('tab', { name: 'Structure' })).toHaveAttribute(
      'aria-selected',
      'true',
    )
  })

  it('adds the real Material name to the Memory header without a duplicate detail request', async () => {
    const requests: string[] = []
    let resolveDetail: ((response: Response) => void) | undefined
    const detailResponse = new Promise<Response>((resolve) => {
      resolveDetail = resolve
    })
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string | URL | Request) => {
        const url = String(input)
        requests.push(url)
        if (url.includes('/memory/story')) {
          return Promise.resolve(
            jsonResponse({
              material_id: material.material_id,
              material_type: 'video',
              tab: 'story',
              payload: {
                title: 'Do not use this analysis title',
                chronological_story_beats: [],
                character_arcs: [],
                themes: [],
              },
            }),
          )
        }
        if (url === '/api/materials/mat_video') return detailResponse
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
      { initialEntries: ['/materials/video/mat_video/memory/story'] },
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    const dialog = await screen.findByRole('dialog', {
      name: 'Material Memory Explorer',
    })
    expect(
      within(dialog).getByRole('heading', { name: 'Material Memory Explorer' }),
    ).toBeVisible()
    expect(within(dialog).queryByText(/\.\.\.|…/)).not.toBeInTheDocument()

    resolveDetail?.(jsonResponse(material))

    const fullTitle = await within(dialog).findByRole('heading', {
      name: 'Material Memory Explorer · La La Land',
    })
    expect(fullTitle).toBeVisible()
    expect(dialog).toHaveAccessibleName('Material Memory Explorer · La La Land')
    expect(fullTitle.querySelector('.memory-modal__title-name')).toHaveTextContent(
      'La La Land',
    )
    expect(requests.filter((url) => url === '/api/materials/mat_video')).toHaveLength(1)
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
    fireEvent.click(screen.getByRole('tab', { name: 'Timeline' }))
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

  it('preserves list filters while Shot selection and Segment return update the URL and player', async () => {
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
              payload: {
                source: { duration_sec: 120, title: 'Source' },
                segments: {
                  items: [
                    {
                      segment_id: 'segment_0010',
                      time_range: { start_sec: 10, end_sec: 20 },
                      segment_summary: 'The selected Segment.',
                      shots: [
                        {
                          shot_id: 'shot_00002',
                          time_range: { start_sec: 15.5, end_sec: 18 },
                          visual_description: 'The selected Shot.',
                        },
                      ],
                    },
                  ],
                  total: 1,
                },
              },
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
      {
        initialEntries: [
          '/materials/video/mat_video/memory/timeline?search=La&sort=name_desc&segment=segment_0010',
        ],
      },
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    const { container } = render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )
    const shotButton = await screen.findByRole('button', { name: /shot_00002/ })

    fireEvent.click(shotButton)
    await waitFor(() =>
      expect(router.state.location).toMatchObject({
        pathname: '/materials/video/mat_video/memory/timeline',
        search: '?search=La&sort=name_desc&segment=segment_0010&shot=shot_00002',
      }),
    )
    await waitFor(() =>
      expect(
        container.querySelector<HTMLVideoElement>('.timeline-explorer__media video')
          ?.currentTime,
      ).toBe(15.5),
    )

    fireEvent.click(screen.getByRole('button', { name: 'Back to Segment' }))
    await waitFor(() =>
      expect(router.state.location).toMatchObject({
        pathname: '/materials/video/mat_video/memory/timeline',
        search: '?search=La&sort=name_desc&segment=segment_0010',
      }),
    )
    await waitFor(() =>
      expect(
        container.querySelector<HTMLVideoElement>('.timeline-explorer__media video')
          ?.currentTime,
      ).toBe(10),
    )
  })

  it('restores an exact Story evidence Segment URL and loads its later page', async () => {
    const requests: string[] = []
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string | URL | Request) => {
        const url = String(input)
        requests.push(url)
        if (url.includes('/memory/story')) {
          return Promise.resolve(
            jsonResponse({
              material_id: material.material_id,
              material_type: 'video',
              tab: 'story',
              limit: 100,
              offset: 0,
              payload: {
                title: 'Evidence story',
                chronological_story_beats: [
                  {
                    summary: 'A late turning point.',
                    source_segment_ids: ['segment_0101'],
                  },
                ],
                character_arcs: [],
                themes: [],
              },
            }),
          )
        }
        if (url.includes('/memory/timeline')) {
          const offset = Number(
            new URL(url, 'http://localhost').searchParams.get('offset'),
          )
          return Promise.resolve(
            jsonResponse({
              material_id: material.material_id,
              material_type: 'video',
              tab: 'timeline',
              limit: 100,
              offset,
              payload: {
                source: { duration_sec: 500, title: 'Source' },
                segments: {
                  items: [
                    offset === 100
                      ? {
                          segment_id: 'segment_0101',
                          time_range: { start_sec: 400, end_sec: 410 },
                          segment_summary: 'The exact evidence Segment.',
                          shots: [],
                        }
                      : {
                          segment_id: 'segment_0001',
                          time_range: { start_sec: 0, end_sec: 10 },
                          segment_summary: 'The opening.',
                          shots: [],
                        },
                  ],
                  total: 101,
                  limit: 100,
                  offset,
                },
              },
            }),
          )
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

    fireEvent.click(await screen.findByRole('button', { name: 'segment_0101' }))
    await waitFor(() =>
      expect(router.state.location).toMatchObject({
        pathname: '/materials/video/mat_video/memory/timeline',
        search: '?search=La&sort=name_desc&segment=segment_0101',
      }),
    )
    expect(await screen.findByRole('heading', { name: 'segment_0101' })).toBeVisible()
    expect(
      requests.some(
        (url) =>
          url.includes('/memory/timeline') &&
          url.includes('limit=100') &&
          url.includes('offset=100'),
      ),
    ).toBe(true)
  })

  it('loads the next Dialogue page without replacing the first page', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn((input: string | URL | Request) => {
        const url = String(input)
        if (url.includes('/memory/dialogue')) {
          const offset = Number(
            new URL(url, 'http://localhost').searchParams.get('offset'),
          )
          return Promise.resolve(
            jsonResponse({
              material_id: material.material_id,
              material_type: 'video',
              tab: 'dialogue',
              limit: 100,
              offset,
              payload: {
                sentences: {
                  items: [
                    offset === 100
                      ? {
                          sentence_id: 101,
                          start: '00:01:40,000',
                          end: '00:01:42,000',
                          text: 'Later page line.',
                        }
                      : {
                          sentence_id: 1,
                          start: '00:00:01,000',
                          end: '00:00:02,000',
                          text: 'First page line.',
                        },
                  ],
                  total: 101,
                  limit: 100,
                  offset,
                },
              },
            }),
          )
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
      { initialEntries: ['/materials/video/mat_video/memory/dialogue'] },
    )
    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={queryClient}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('First page line.')).toBeVisible()
    fireEvent.click(screen.getByRole('button', { name: 'Load more' }))
    expect(await screen.findByText('Later page line.')).toBeVisible()
    expect(screen.getByText('First page line.')).toBeVisible()
  })
})
