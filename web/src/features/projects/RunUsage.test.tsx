import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  ProjectLayout,
  ProjectRuns,
  RunDetail,
} from '@/features/projects/ProjectWorkspace'
import i18n from '@/i18n'

const project = {
  project_id: 'project_1',
  name: 'Usage Film',
  video_material_ids: ['mat_video'],
  music_material_ids: ['mat_music'],
  creative_brief: {
    editing_intent: 'Preserve the emotional arc.',
    target_duration_sec: 60,
  },
  created_at: '2026-08-13T00:00:00Z',
  updated_at: '2026-08-13T00:01:00Z',
}

const run = {
  run_id: 'run_1',
  project_id: project.project_id,
  sequence: 1,
  status: 'complete',
  creative_brief: project.creative_brief,
  video_material_ids: ['mat_video'],
  music_material_ids: ['mat_music'],
  failure_message: null,
  created_at: '2026-08-13T00:00:00Z',
  updated_at: '2026-08-13T00:01:00Z',
}

const bucket = {
  request_count: 2,
  reported_usage_count: 1,
  unreported_usage_count: 1,
  priced_usage_count: 1,
  unpriced_usage_count: 0,
  prompt_tokens: 100,
  completion_tokens: 20,
  total_tokens: 120,
  cached_prompt_tokens: 30,
  uncached_prompt_tokens: 70,
  reasoning_tokens: 5,
  uncached_input_cost_yuan: 0.0048,
  cached_input_cost_yuan: 0.0012,
  output_cost_yuan: 0.006,
  total_cost_yuan: 0.012,
}

const fullSummary = {
  ...bucket,
  currency: 'CNY',
  price_unit: 'yuan_per_million_tokens',
  by_model: { 'qwen3.7-max': bucket },
  by_task: {
    candidate_retrieval: bucket,
    candidate_visual_scoring: bucket,
    dialogue_anchor_selection: bucket,
    pairwise_scoring: bucket,
    script_review: bucket,
    revision_editor: bucket,
    slot_arrangement: bucket,
  },
}

const compactSummary = {
  ...bucket,
  currency: 'CNY',
  price_unit: 'yuan_per_million_tokens',
}

const modelUsage = {
  schema_version: '1.0',
  attempt_usage: [
    {
      attempt_id: 'attempt_1',
      sequence: 1,
      status: 'complete',
      model_usage_summary: fullSummary,
    },
    {
      attempt_id: 'attempt_2',
      sequence: 2,
      status: 'interrupted',
      model_usage_summary: null,
    },
  ],
  run_total: fullSummary,
}

function jsonResponse(value: unknown) {
  return new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

function queryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: Number.POSITIVE_INFINITY },
      mutations: { retry: false },
    },
  })
}

beforeEach(async () => {
  await i18n.changeLanguage('en-US')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Run model usage projection', () => {
  it('shows full Attempt, model, task, and reporting detail on the Run page', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse({
          run,
          frozen_edits: [],
          execution: null,
          model_usage: modelUsage,
        }),
      ),
    )
    const router = createMemoryRouter(
      [{ path: '/runs/:runId', element: <RunDetail /> }],
      { initialEntries: ['/runs/run_1'] },
    )
    render(
      <QueryClientProvider client={queryClient()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByRole('heading', { name: 'Model usage' })).toBeVisible()
    expect(screen.getByText('Attempt #1')).toBeVisible()
    expect(screen.getByText('Attempt #2')).toBeVisible()
    expect(
      screen.getByText('Usage could not be recorded for this Attempt.'),
    ).toBeVisible()
    expect(screen.getByText('qwen3.7-max')).toBeVisible()
    expect(screen.getByText('slot_arrangement')).toBeVisible()
    expect(screen.getByLabelText('Arrangement Architect')).toHaveTextContent('A')
    expect(screen.getByLabelText('Story Editor')).toHaveTextContent('S')
    expect(screen.getAllByLabelText('Timeline Scout')).toHaveLength(1)
    expect(screen.getByLabelText('Edit Composer')).toHaveTextContent('E')
    expect(screen.queryByLabelText('Revision Editor (legacy)')).not.toBeInTheDocument()
    expect(screen.queryByText('script_review')).not.toBeInTheDocument()
    expect(screen.queryByText('revision_editor')).not.toBeInTheDocument()
    const taskSection = screen
      .getByRole('heading', { name: 'By ASTER task' })
      .closest('section')
    expect(
      Array.from(
        taskSection?.querySelectorAll('.run-usage__task-row strong') ?? [],
      ).map((item) => item.textContent),
    ).toEqual([
      'slot_arrangement',
      'dialogue_anchor_selection',
      'candidate_retrieval',
      'candidate_visual_scoring',
      'pairwise_scoring',
    ])
    expect(taskSection?.querySelectorAll('.run-usage__task-group')).toHaveLength(4)
    const timelineScoutGroup = screen
      .getByLabelText('Timeline Scout')
      .closest('article')
    expect(timelineScoutGroup).toHaveTextContent('candidate_retrieval')
    expect(timelineScoutGroup).toHaveTextContent('candidate_visual_scoring')
    expect(
      screen.getByText(/Model requests without reported token usage: 1/),
    ).toBeVisible()
    expect(screen.getAllByText(/120 tokens · CN¥0\.012/).length).toBeGreaterThan(0)
  })

  it('keeps the Project Run list compact to tokens and CNY cost', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ items: [] })),
    )
    const client = queryClient()
    client.setQueryData(['project-workspace', project.project_id], {
      project,
      materials: { video: [], music: [] },
      runs: [],
    })
    client.setQueryData(['project-runs', project.project_id], {
      items: [
        {
          run,
          execution: null,
          model_usage_total: compactSummary,
        },
      ],
    })
    const router = createMemoryRouter(
      [
        {
          path: '/projects/:projectId',
          element: <ProjectLayout />,
          children: [{ path: 'runs', element: <ProjectRuns /> }],
        },
      ],
      { initialEntries: ['/projects/project_1/runs'] },
    )
    render(
      <QueryClientProvider client={client}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByText('120 tokens · CN¥0.012')).toBeVisible()
    expect(screen.queryByText('qwen3.7-max')).not.toBeInTheDocument()
    expect(screen.queryByText('story_editor')).not.toBeInTheDocument()
  })
})
