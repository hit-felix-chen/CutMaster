/// <reference types="node" />

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import {
  afterAll,
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from 'vitest'

import { MaterialsWorkspace } from '@/features/materials/MaterialsWorkspace'
import { MusicStructureView } from '@/features/materials/memory/MemoryViews'
import i18n from '@/i18n'

const workspaceCss = readFileSync(
  resolve(process.cwd(), 'src/styles/workspace.css'),
  'utf8',
)

let stylesheet: HTMLStyleElement

function musicPayload(duration: number) {
  return {
    source_duration_sec: duration,
    tempo_bpm: 120,
    beats_sec: { items: [0, 60], total: 174 },
    accents_sec: { items: [30], total: 52 },
    energy_curve: { items: [], total: 0 },
    sections: {
      items: [
        {
          section_id: 'section_1',
          start_sec: 0,
          end_sec: duration,
          role: 'development',
        },
      ],
      total: 1,
    },
  }
}

function jsonResponse(value: unknown) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

beforeAll(() => {
  stylesheet = document.createElement('style')
  stylesheet.textContent = workspaceCss
  document.head.append(stylesheet)
})

afterAll(() => stylesheet.remove())

beforeEach(async () => {
  await i18n.changeLanguage('en-US')
})

afterEach(() => vi.unstubAllGlobals())

describe('Music Structure timeline window', () => {
  it('keeps the label column fixed and shares one coordinate plane with a one-minute axis', () => {
    const { container } = render(
      <MusicStructureView materialId="mat_music" payload={musicPayload(630)} />,
    )

    const timeline = screen.getByRole('region', { name: 'Music timeline' })
    const labels = timeline.querySelector<HTMLElement>('.music-timeline__labels')
    const scroll = timeline.querySelector<HTMLElement>('.music-timeline__scroll')
    const canvas = timeline.querySelector<HTMLElement>('.music-timeline__canvas')
    const axis = timeline.querySelector<HTMLElement>('.music-timeline__axis')
    const playhead = timeline.querySelector<HTMLElement>('.music-timeline__playhead')

    expect(labels).not.toBeNull()
    expect(scroll).not.toBeNull()
    expect(canvas).not.toBeNull()
    expect(axis).not.toBeNull()
    expect(playhead).not.toBeNull()
    expect(labels?.parentElement).toBe(scroll?.parentElement)
    expect(scroll).not.toContainElement(labels)
    expect(Array.from(scroll?.children ?? [])).toEqual([canvas])
    expect(canvas).toContainElement(axis)
    expect(canvas).toContainElement(playhead)
    expect(canvas?.querySelectorAll('.music-timeline__row')).toHaveLength(3)

    const fixedLabels = within(labels!).getAllByTestId('music-timeline-label')
    expect(fixedLabels).toHaveLength(4)
    expect(fixedLabels.map((item) => item.firstElementChild?.textContent)).toEqual([
      'Sections',
      'Beats',
      'Accents',
      'Timeline',
    ])
    expect(within(labels!).getByText('2 / 174')).toBeVisible()
    expect(within(labels!).getByText('1 / 52')).toBeVisible()
    expect(timeline.querySelector(':scope > footer')).toBeNull()

    expect(
      Array.from(axis!.querySelectorAll('time'), (item) => item.textContent),
    ).toEqual([
      '00:00',
      '1:00',
      '2:00',
      '3:00',
      '4:00',
      '5:00',
      '6:00',
      '7:00',
      '8:00',
      '9:00',
      '10:00',
      '10:30',
    ])
    expect(timeline).toHaveClass('music-timeline--scrollable')
    expect(canvas).toHaveStyle({ width: '105%' })
    expect(getComputedStyle(scroll!).overflowX).toBe('auto')
    expect(playhead).toHaveStyle({ left: '0%' })

    const beatButtons = canvas!.querySelectorAll<HTMLButtonElement>(
      '.music-timeline__row--beats button',
    )
    expect(Array.from(beatButtons, (button) => button.tabIndex)).toEqual([0, -1])
    fireEvent.keyDown(beatButtons[0]!, { key: 'ArrowRight' })
    expect(beatButtons[1]).toHaveFocus()
    expect(container.querySelector('audio')?.currentTime).toBe(60)

    const audio = container.querySelector('audio')
    if (audio) {
      audio.currentTime = 630
      fireEvent.timeUpdate(audio)
    }
    expect(playhead).toHaveStyle({ left: '100%' })
  })

  it('scales a timeline of exactly ten minutes to the available width', () => {
    render(<MusicStructureView materialId="mat_music" payload={musicPayload(600)} />)

    const timeline = screen.getByRole('region', { name: 'Music timeline' })
    const canvas = timeline.querySelector<HTMLElement>('.music-timeline__canvas')
    const axis = timeline.querySelector<HTMLElement>('.music-timeline__axis')

    expect(timeline).not.toHaveClass('music-timeline--scrollable')
    expect(canvas).toHaveStyle({ width: '100%' })
    expect(
      Array.from(axis!.querySelectorAll('time'), (item) => item.textContent),
    ).toEqual([
      '00:00',
      '1:00',
      '2:00',
      '3:00',
      '4:00',
      '5:00',
      '6:00',
      '7:00',
      '8:00',
      '9:00',
      '10:00',
    ])
  })

  it('keeps the exact end timestamp without overlapping a nearby minute tick', () => {
    render(<MusicStructureView materialId="mat_music" payload={musicPayload(601)} />)

    const timeline = screen.getByRole('region', { name: 'Music timeline' })
    const axis = timeline.querySelector<HTMLElement>('.music-timeline__axis')
    const labels = Array.from(
      axis!.querySelectorAll('time'),
      (item) => item.textContent,
    )

    expect(labels.at(-2)).toBe('10:00')
    expect(labels.at(-1)).toBe('10:01')
    expect(axis?.querySelector('.music-timeline__tick--end')).toHaveClass(
      'music-timeline__tick--staggered',
    )
  })
})

describe('Music Structure full loading', () => {
  it('automatically fetches every 500-item page and never offers Load more', async () => {
    const memoryRequests: URL[] = []
    const beats = Array.from({ length: 501 }, (_, index) => index)
    const energy = beats.map((time) => ({ time_sec: time, energy: 0.5 }))
    const accents = [30, 90]
    const sections = [
      { section_id: 'intro', start_sec: 0, end_sec: 60, role: 'intro' },
      {
        section_id: 'development',
        start_sec: 60,
        end_sec: 620,
        role: 'development',
      },
    ]
    const page = (items: unknown[], limit: number, offset: number) => ({
      items: items.slice(offset, offset + limit),
      total: items.length,
      limit,
      offset,
    })

    vi.stubGlobal(
      'fetch',
      vi.fn((input: string | URL | Request) => {
        const url = new URL(String(input), 'http://localhost')
        if (url.pathname.endsWith('/memory/structure')) {
          memoryRequests.push(url)
          const limit = Number(url.searchParams.get('limit'))
          const offset = Number(url.searchParams.get('offset'))
          return Promise.resolve(
            jsonResponse({
              material_id: 'mat_music',
              material_type: 'music',
              tab: 'structure',
              limit,
              offset,
              payload: {
                source_duration_sec: 620,
                tempo_bpm: 120,
                beats_sec: page(beats, limit, offset),
                accents_sec: page(accents, limit, offset),
                energy_curve: page(energy, limit, offset),
                sections: page(sections, limit, offset),
              },
            }),
          )
        }
        if (url.pathname === '/api/materials/mat_music') {
          return Promise.resolve(
            jsonResponse({
              material_id: 'mat_music',
              material_type: 'music',
              name: 'Long Score',
              condition: 'ready',
              duration_sec: 620,
              analysis_available: true,
              reference_count: 0,
              references: [],
            }),
          )
        }
        if (url.pathname === '/api/materials') {
          return Promise.resolve(
            jsonResponse({
              items: [
                {
                  material_id: 'mat_music',
                  material_type: 'music',
                  name: 'Long Score',
                  condition: 'ready',
                  duration_sec: 620,
                  analysis_available: true,
                  reference_count: 0,
                },
              ],
              total: 1,
            }),
          )
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

    await screen.findByRole('region', { name: 'Music timeline' })
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument()
    expect(memoryRequests[0]?.searchParams.get('limit')).toBe('500')
    expect(memoryRequests[0]?.searchParams.get('offset')).toBe('0')

    expect(
      await screen.findByRole('button', { name: 'Seek to beat at 8:20' }),
    ).toBeVisible()
    await waitFor(() => expect(memoryRequests).toHaveLength(2))
    expect(memoryRequests[1]?.searchParams.get('limit')).toBe('500')
    expect(memoryRequests[1]?.searchParams.get('offset')).toBe('500')
    expect(screen.queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument()
  }, 15_000)

  it('hides partial pages after an automatic page failure and completes after retry', async () => {
    const beats = Array.from({ length: 501 }, (_, index) => index)
    const energy = beats.map((time) => ({ time_sec: time, energy: 0.5 }))
    let secondPageAttempts = 0
    const page = (items: unknown[], limit: number, offset: number) => ({
      items: items.slice(offset, offset + limit),
      total: items.length,
      limit,
      offset,
    })

    vi.stubGlobal(
      'fetch',
      vi.fn((input: string | URL | Request) => {
        const url = new URL(String(input), 'http://localhost')
        if (url.pathname.endsWith('/memory/structure')) {
          const limit = Number(url.searchParams.get('limit'))
          const offset = Number(url.searchParams.get('offset'))
          if (offset === 500) {
            secondPageAttempts += 1
            if (secondPageAttempts === 1) {
              return Promise.resolve(
                new Response(
                  JSON.stringify({
                    code: 'music_memory_page_failed',
                    detail: 'The second page failed.',
                  }),
                  {
                    status: 500,
                    headers: { 'Content-Type': 'application/problem+json' },
                  },
                ),
              )
            }
          }
          return Promise.resolve(
            jsonResponse({
              material_id: 'mat_music',
              material_type: 'music',
              tab: 'structure',
              limit,
              offset,
              payload: {
                source_duration_sec: 620,
                tempo_bpm: 120,
                beats_sec: page(beats, limit, offset),
                accents_sec: page([30, 90], limit, offset),
                energy_curve: page(energy, limit, offset),
                sections: page(
                  [
                    {
                      section_id: 'intro',
                      start_sec: 0,
                      end_sec: 620,
                      role: 'intro',
                    },
                  ],
                  limit,
                  offset,
                ),
              },
            }),
          )
        }
        if (url.pathname === '/api/materials/mat_music') {
          return Promise.resolve(
            jsonResponse({
              material_id: 'mat_music',
              material_type: 'music',
              name: 'Recoverable Score',
              condition: 'ready',
              duration_sec: 620,
              analysis_available: true,
              reference_count: 0,
              references: [],
            }),
          )
        }
        if (url.pathname === '/api/materials') {
          return Promise.resolve(
            jsonResponse({
              items: [
                {
                  material_id: 'mat_music',
                  material_type: 'music',
                  name: 'Recoverable Score',
                  condition: 'ready',
                  duration_sec: 620,
                  analysis_available: true,
                  reference_count: 0,
                },
              ],
              total: 1,
            }),
          )
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
    const error = await within(dialog).findByRole('alert')
    expect(within(error).getByText('Unable to load')).toBeVisible()
    expect(
      screen.queryByRole('region', { name: 'Music timeline' }),
    ).not.toBeInTheDocument()
    expect(screen.queryByText('Loading')).not.toBeInTheDocument()
    expect(secondPageAttempts).toBe(1)

    fireEvent.click(within(error).getByRole('button', { name: 'Try again' }))

    expect(
      await screen.findByRole('button', { name: 'Seek to beat at 8:20' }),
    ).toBeVisible()
    expect(secondPageAttempts).toBe(2)
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByText('Loading')).not.toBeInTheDocument()
    const labels = screen
      .getByRole('region', { name: 'Music timeline' })
      .querySelector<HTMLElement>('.music-timeline__labels')
    expect(within(labels!).getByText('501 / 501')).toBeVisible()
  }, 15_000)
})
