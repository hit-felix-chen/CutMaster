import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ReviewWorkspace } from '@/features/review/ReviewWorkspace'
import type { FrozenEditReview } from '@/features/shared/api'
import i18n from '@/i18n'

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function reviewFixture(
  options: { editId?: string; sequence?: number; candidateSpace?: boolean } = {},
): FrozenEditReview {
  const editId = options.editId ?? 'edit_1'
  const sequence = options.sequence ?? 1
  const candidateSpace = options.candidateSpace ?? false
  const edit = {
    edit_id: editId,
    run_id: 'run_1',
    sequence,
    origin: sequence === 1 ? 'initial' : 'guided_revision',
    parent_edit_id: sequence === 1 ? null : 'edit_1',
    created_at: '2026-08-13T00:01:00Z',
  }
  return {
    edit,
    run: {
      run_id: 'run_1',
      project_id: 'project_1',
      sequence: 1,
      status: 'complete',
      creative_brief: {
        editing_intent: 'Preserve the emotional arc.',
        target_duration_sec: 8,
      },
      video_material_ids: ['mat_video'],
      music_material_ids: ['mat_music'],
      created_at: '2026-08-13T00:00:00Z',
      updated_at: '2026-08-13T00:01:00Z',
    },
    versions: [edit],
    plan: {
      schema_version: '2.0',
      plan_id: 'plan_1',
      fps: 30,
      total_frames: 240,
      duration_sec: 8,
    },
    media: {
      video: {
        material_id: 'mat_video',
        source_url: '/api/materials/mat_video/source',
      },
      music: {
        material_id: 'mat_music',
        source_url: '/api/materials/mat_music/source',
      },
    },
    candidate_space_available: candidateSpace,
    candidate_space_unavailable_reason: candidateSpace ? null : 'not_persisted',
    slots: [
      {
        slot_id: 'slot_01',
        position: 1,
        is_anchor: true,
        output_start_sec: 0,
        output_end_sec: 4,
        source_start_sec: 10,
        source_end_sec: 14,
        source_timestamp: '00:00:10,000-00:00:14,000',
        selected_candidate_id: 'anchor_1',
        picture: 'The opening line establishes the story.',
        selection_scores: { unary: 0.9 },
        dialogue_anchor: {
          text: 'We have to leave.',
          dialogue_items: [
            {
              speaker: 'Mia',
              text: 'We have to leave.',
              time_range: { start_sec: 10, end_sec: 12 },
            },
          ],
        },
      },
      {
        slot_id: 'slot_02',
        position: 2,
        is_anchor: false,
        output_start_sec: 4,
        output_end_sec: 8,
        source_start_sec: 20,
        source_end_sec: 24,
        source_timestamp: '00:00:20,000-00:00:24,000',
        selected_candidate_id: 'candidate_a',
        picture: 'The pair cross the city at night.',
        selection_scores: { unary: 0.82 },
        dialogue_anchor: null,
      },
    ],
    candidates: {
      slot_01: [
        {
          candidate_id: 'anchor_1',
          slot_id: 'slot_01',
          source_start_sec: 10,
          source_end_sec: 14,
          source_timestamp: '00:00:10,000-00:00:14,000',
          description: 'The locked original dialogue.',
          semantic_relevance: 0.9,
          visual_score: 0.8,
          protagonist_visibility_score: 0.8,
          emotional_intensity: 0.7,
          kinetic_energy: 0.4,
          salience: 1,
          visual_evidence: null,
          selected: true,
          eligible_for_replacement: false,
          media_url: '/api/materials/mat_video/source',
        },
      ],
      slot_02: [
        {
          candidate_id: 'candidate_a',
          slot_id: 'slot_02',
          source_start_sec: 20,
          source_end_sec: 24,
          source_timestamp: '00:00:20,000-00:00:24,000',
          description: 'The selected city crossing.',
          semantic_relevance: 0.82,
          visual_score: 0.75,
          protagonist_visibility_score: 0.8,
          emotional_intensity: 0.65,
          kinetic_energy: 0.55,
          salience: 0.8,
          visual_evidence: 'Both protagonists remain visible.',
          selected: true,
          eligible_for_replacement: candidateSpace,
          media_url: '/api/materials/mat_video/source',
        },
        ...(candidateSpace
          ? [
              {
                candidate_id: 'candidate_b',
                slot_id: 'slot_02',
                source_start_sec: 30,
                source_end_sec: 34,
                source_timestamp: '00:00:30,000-00:00:34,000',
                description: 'A quieter alternate city crossing.',
                semantic_relevance: 0.9,
                visual_score: 0.92,
                protagonist_visibility_score: 0.9,
                emotional_intensity: 0.7,
                kinetic_energy: 0.4,
                salience: 0.85,
                visual_evidence: 'The alternate preserves screen direction.',
                selected: false,
                eligible_for_replacement: true,
                media_url: '/api/materials/mat_video/source',
              },
            ]
          : []),
      ],
    },
    variants: [],
    timeline: {
      dialogue_cues: [
        {
          slot_id: 'slot_01',
          start_sec: 0,
          end_sec: 2,
          text: 'We have to leave.',
          speaker: 'Mia',
        },
      ],
      music_beats_sec: [0, 2, 4, 6],
      music_beats_available: true,
    },
  }
}

function renderReview(initialEntry: string) {
  const router = createMemoryRouter(
    [
      {
        path: '/projects/:projectId/runs/:runId/review/:editId',
        element: <ReviewWorkspace />,
      },
      {
        path: '/projects/:projectId/runs/:runId',
        element: <h1>Run detail</h1>,
      },
    ],
    { initialEntries: [initialEntry] },
  )
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

beforeEach(async () => {
  await i18n.changeLanguage('en-US')
  vi.spyOn(HTMLMediaElement.prototype, 'play').mockResolvedValue()
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined)
})

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('Review workspace', () => {
  it('renders a historical Frozen Edit from real projection data and simulates its source sequence', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(reviewFixture())),
    )
    const router = renderReview(
      '/projects/project_1/runs/run_1/review/edit_1?slot=slot_01',
    )

    expect(
      await screen.findByText('The opening line establishes the story.'),
    ).toBeVisible()
    expect(screen.getByText('Story Anchor')).toBeVisible()
    expect(screen.getByText(/created before Candidate Space persistence/)).toBeVisible()
    expect(document.querySelectorAll('video')).toHaveLength(1)
    expect(document.querySelector('video')).toHaveAttribute(
      'src',
      '/api/materials/mat_video/source',
    )
    expect(
      screen.getByText('Source sequence simulation · not rendered output'),
    ).toBeVisible()

    const video = document.querySelector('video') as HTMLVideoElement
    video.currentTime = 14
    fireEvent.timeUpdate(video)

    await waitFor(() => expect(router.state.location.search).toContain('slot=slot_02'))
    expect(await screen.findByText('The pair cross the city at night.')).toBeVisible()
  })

  it('submits all replacements atomically and opens the derived Frozen Edit', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (init?.method === 'POST') {
        return jsonResponse(
          {
            frozen_edit: reviewFixture({ editId: 'edit_2', sequence: 2 }).edit,
            review_url: '/projects/project_1/runs/run_1/review/edit_2',
          },
          201,
        )
      }
      return jsonResponse(
        url.endsWith('/edit_2/review')
          ? reviewFixture({ editId: 'edit_2', sequence: 2, candidateSpace: true })
          : reviewFixture({ candidateSpace: true }),
      )
    })
    vi.stubGlobal('fetch', fetchMock)
    const router = renderReview(
      '/projects/project_1/runs/run_1/review/edit_1?slot=slot_02',
    )
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Use candidate' }))
    expect(screen.getByText('Unsaved changes')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Save revision' }))

    await waitFor(() =>
      expect(router.state.location.pathname).toBe(
        '/projects/project_1/runs/run_1/review/edit_2',
      ),
    )
    const postCall = fetchMock.mock.calls.find(([, init]) => init?.method === 'POST')
    expect(postCall).toBeDefined()
    expect(postCall?.[0]).toBe('/api/frozen-edits/edit_1/revisions')
    expect(JSON.parse(String(postCall?.[1]?.body))).toEqual({
      replacements: [{ slot_id: 'slot_02', candidate_id: 'candidate_b' }],
    })
    expect(postCall?.[1]?.headers).toMatchObject({
      'Idempotency-Key': expect.any(String),
    })
  })

  it('blocks leaving a dirty revision and supports accessible discard confirmation', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(reviewFixture({ candidateSpace: true }))),
    )
    const router = renderReview(
      '/projects/project_1/runs/run_1/review/edit_1?slot=slot_02',
    )
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Use candidate' }))
    await user.click(screen.getByRole('link', { name: 'Back to Run' }))

    const stay = screen.getByRole('button', { name: 'Stay on page' })
    expect(stay).toHaveFocus()
    await user.tab({ shift: true })
    expect(
      screen.getByRole('button', { name: 'Discard changes and leave' }),
    ).toHaveFocus()
    await user.tab()
    expect(stay).toHaveFocus()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(router.state.location.pathname).toContain('/review/edit_1')

    await user.click(screen.getByRole('link', { name: 'Back to Run' }))
    await user.click(screen.getByRole('button', { name: 'Discard changes and leave' }))
    expect(await screen.findByRole('heading', { name: 'Run detail' })).toBeVisible()
  })
})
