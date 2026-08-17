import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ReviewWorkspace } from '@/features/review/ReviewWorkspace'
import type {
  ExecutionSummary,
  FrozenEditReview,
  RenderVariant,
  RenderVariantListItem,
} from '@/features/shared/api'
import i18n from '@/i18n'

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function reviewFixture(
  options: { editId?: string; sequence?: number } = {},
): FrozenEditReview {
  const editId = options.editId ?? 'edit_1'
  const sequence = options.sequence ?? 1
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
          eligible_for_replacement: false,
          media_url: '/api/materials/mat_video/source',
        },
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

function renderVariant(
  status: RenderVariant['status'],
  audioMode: RenderVariant['specification']['audio_mode'] = 'dialogue',
): RenderVariant {
  return {
    render_variant_id: `variant_${status}_${audioMode}`,
    project_id: 'project_1',
    run_id: 'run_1',
    run_sequence: 1,
    edit_id: 'edit_1',
    edit_sequence: 1,
    edit_origin: 'initial',
    status,
    specification: {
      schema_version: '1.0',
      audio_mode: audioMode,
      renderer: {
        width: 1920,
        height: 1080,
        fps: 30,
        encoder: 'libx264',
        threads: 8,
        bgm_volume: 0.3,
        original_volume: 0,
        audio_sample_rate: 48000,
        dialogue_audio: {},
      },
    },
    frame_count: status === 'ready' ? 240 : null,
    duration_sec: status === 'ready' ? 8 : null,
    size_bytes: status === 'ready' ? 12_345 : null,
    failure_message: status === 'failed' ? 'ffmpeg failed' : null,
    media_url:
      status === 'ready'
        ? `/api/render-variants/variant_${status}_${audioMode}/media`
        : null,
    download_url:
      status === 'ready'
        ? `/api/render-variants/variant_${status}_${audioMode}/download`
        : null,
    created_at: '2026-08-13T00:02:00Z',
    updated_at: '2026-08-13T00:02:00Z',
  }
}

function renderExecution(status: string): ExecutionSummary {
  return {
    attempt: {
      attempt_id: 'attempt_render_1',
      operation_type: 'renderer',
      owner_type: 'render_variant',
      owner_id: 'variant_1',
      sequence: 1,
      status,
      created_at: '2026-08-13T00:02:00Z',
      updated_at: '2026-08-13T00:02:00Z',
    },
    job: {
      job_id: 'job_render_1',
      attempt_id: 'attempt_render_1',
      status,
      stop_requested: false,
      progress: {},
      created_at: '2026-08-13T00:02:00Z',
      updated_at: '2026-08-13T00:02:00Z',
    },
  }
}

function renderItem(
  status: RenderVariant['status'],
  audioMode: RenderVariant['specification']['audio_mode'] = 'dialogue',
): RenderVariantListItem {
  return {
    render_variant: renderVariant(status, audioMode),
    execution:
      status === 'queued' || status === 'rendering'
        ? renderExecution(status === 'queued' ? 'queued' : 'running')
        : null,
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
  it('accepts a complete Candidate Bundle without legacy availability fields', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse(reviewFixture())),
    )
    renderReview('/projects/project_1/runs/run_1/review/edit_1?slot=slot_02')
    const user = userEvent.setup()

    expect(await screen.findByText('A quieter alternate city crossing.')).toBeVisible()
    expect(
      screen.queryByText(
        /Candidate Space is unavailable|before Candidate Space persistence/,
      ),
    ).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Use candidate' }))
    expect(screen.getByText('Unsaved changes')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Save' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Undo changes' })).toBeEnabled()
  })

  it.each([
    {
      locale: 'en-US',
      message: 'A required Review artifact is unavailable or failed validation.',
    },
    {
      locale: 'zh-CN',
      message: '审阅所需的产物不可用或校验失败。',
    },
  ])(
    'shows the localised Review artifact error for a 409 in $locale',
    async ({ locale, message }) => {
      await i18n.changeLanguage(locale)
      vi.stubGlobal(
        'fetch',
        vi.fn(async (input: RequestInfo | URL) =>
          String(input).endsWith('/api/frozen-edits/edit_1/review')
            ? jsonResponse(
                {
                  code: 'review_artifact_unavailable',
                  status: 409,
                  detail: 'private artifact path and validation detail',
                },
                409,
              )
            : jsonResponse({ items: [], count: 0 }),
        ),
      )
      renderReview('/projects/project_1/runs/run_1/review/edit_1?slot=slot_02')

      expect(await screen.findByText(message)).toBeVisible()
      expect(
        screen.queryByText('private artifact path and validation detail'),
      ).not.toBeInTheDocument()
      expect(screen.queryByText('The selected city crossing.')).not.toBeInTheDocument()
      expect(document.querySelector('video')).not.toBeInTheDocument()
    },
  )

  it('renders a Frozen Edit from a complete Candidate Bundle and simulates its source sequence', async () => {
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
          ? reviewFixture({ editId: 'edit_2', sequence: 2 })
          : reviewFixture(),
      )
    })
    vi.stubGlobal('fetch', fetchMock)
    const router = renderReview(
      '/projects/project_1/runs/run_1/review/edit_1?slot=slot_02',
    )
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Use candidate' }))
    expect(screen.getByText('Unsaved changes')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Save' }))

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
      vi.fn(async () => jsonResponse(reviewFixture())),
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

  it('creates an explicit Dialogue Preview and selects its real queued Variant', async () => {
    let items: RenderVariantListItem[] = []
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/frozen-edits/edit_1/review')) {
        return jsonResponse(reviewFixture())
      }
      if (url.endsWith('/api/frozen-edits/edit_1/render-variants')) {
        if (init?.method === 'POST') {
          const queued = renderItem('queued')
          items = [queued]
          return jsonResponse(
            {
              render_variant: queued.render_variant,
              execution: queued.execution,
              created: true,
            },
            202,
          )
        }
        return jsonResponse({ items, count: items.length })
      }
      return new Response(null, { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const router = renderReview(
      '/projects/project_1/runs/run_1/review/edit_1?slot=slot_01',
    )
    const user = userEvent.setup()

    await user.click(
      await screen.findByRole('button', { name: 'Render Dialogue Preview' }),
    )

    await waitFor(() =>
      expect(router.state.location.search).toContain('variant=variant_queued_dialogue'),
    )
    expect(
      screen.getByRole('option', {
        name: 'Variant 1 · Dialogue Preview · Queued',
      }),
    ).toBeVisible()
    expect(await screen.findByText('Variant 1')).toBeVisible()
    expect(screen.queryByText('variant_queued_dialogue')).not.toBeInTheDocument()
    expect((await screen.findAllByText('Queued'))[0]).toBeVisible()
    expect(screen.getByRole('button', { name: 'Stop' })).toBeVisible()
    const createCall = fetchMock.mock.calls.find(
      ([url, init]) =>
        String(url).endsWith('/api/frozen-edits/edit_1/render-variants') &&
        init?.method === 'POST',
    )
    expect(JSON.parse(String(createCall?.[1]?.body))).toEqual({
      audio_mode: 'dialogue',
    })
    expect(createCall?.[1]?.headers).toMatchObject({
      'Idempotency-Key': expect.any(String),
    })
  })

  it('resumes only an Interrupted Variant through its recovery endpoint', async () => {
    let item = renderItem('interrupted', 'bgm_only')
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/frozen-edits/edit_1/review')) {
        return jsonResponse(reviewFixture())
      }
      if (url.endsWith('/api/frozen-edits/edit_1/render-variants')) {
        return jsonResponse({ items: [item], count: 1 })
      }
      if (
        url.endsWith('/api/render-variants/variant_interrupted_bgm_only/resume') &&
        init?.method === 'POST'
      ) {
        item = {
          ...item,
          render_variant: {
            ...item.render_variant,
            status: 'queued',
            updated_at: '2026-08-13T00:03:00Z',
          },
          execution: renderExecution('queued'),
        }
        return jsonResponse(
          {
            render_variant: item.render_variant,
            execution: item.execution,
            created: false,
          },
          202,
        )
      }
      return new Response(null, { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    renderReview(
      '/projects/project_1/runs/run_1/review/edit_1?variant=variant_interrupted_bgm_only',
    )
    const user = userEvent.setup()

    await user.click(await screen.findByRole('button', { name: 'Resume' }))

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/render-variants/variant_interrupted_bgm_only/resume',
        expect.objectContaining({ method: 'POST' }),
      ),
    )
    expect((await screen.findAllByText('Queued'))[0]).toBeVisible()
  })

  it('shows a localised render failure without exposing the worker summary', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: RequestInfo | URL) =>
        String(input).endsWith('/api/frozen-edits/edit_1/review')
          ? jsonResponse(reviewFixture())
          : jsonResponse({ items: [renderItem('failed')], count: 1 }),
      ),
    )
    renderReview(
      '/projects/project_1/runs/run_1/review/edit_1?variant=variant_failed_dialogue',
    )

    expect(
      await screen.findByText(
        'Rendering did not complete. Check the source materials and render settings, then retry.',
      ),
    ).toHaveAttribute('role', 'alert')
    expect(screen.queryByText('ffmpeg failed')).not.toBeInTheDocument()
  })

  it('plays a Ready Variant and deletes it only on the second click without leaving the Edit', async () => {
    let items = [renderItem('ready')]
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      if (url.endsWith('/api/frozen-edits/edit_1/review')) {
        return jsonResponse(reviewFixture())
      }
      if (url.endsWith('/api/frozen-edits/edit_1/render-variants')) {
        return jsonResponse({ items, count: items.length })
      }
      if (
        url.endsWith('/api/render-variants/variant_ready_dialogue') &&
        init?.method === 'DELETE'
      ) {
        items = []
        return jsonResponse({
          render_variant_id: 'variant_ready_dialogue',
          deleted: true,
        })
      }
      return new Response(null, { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
    const router = renderReview(
      '/projects/project_1/runs/run_1/review/edit_1?variant=variant_ready_dialogue',
    )
    const user = userEvent.setup()

    await waitFor(() => expect(document.querySelector('video')).not.toBeNull())
    const video = document.querySelector('video')
    expect(video).toHaveAttribute(
      'src',
      '/api/render-variants/variant_ready_dialogue/media',
    )
    expect(screen.getByRole('link', { name: 'Download copy' })).toHaveAttribute(
      'href',
      '/api/render-variants/variant_ready_dialogue/download',
    )
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'DELETE')).toBe(
      false,
    )
    await user.click(screen.getByRole('button', { name: 'Click again to delete' }))

    await waitFor(() => expect(router.state.location.search).not.toContain('variant='))
    expect(router.state.location.pathname).toBe(
      '/projects/project_1/runs/run_1/review/edit_1',
    )
    expect(await screen.findByText('Source review')).toBeVisible()
  })
})
