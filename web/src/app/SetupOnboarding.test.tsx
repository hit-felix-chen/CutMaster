import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApplicationGate } from '@/app/ApplicationGate'
import { setupReturnTarget } from '@/app/setup-return'
import type {
  AsrProviderConfiguration,
  ModelProviderConfiguration,
  ProviderBundle,
  ProviderCapability,
  ProviderProfile,
  SettingsView,
} from '@/features/shared/api'
import i18n from '@/i18n'
import { SetupPage } from '@/pages/SetupPage'

type Configured = Record<ProviderCapability, boolean>

function model(
  name: string,
  baseUrl: string,
  apiKeyEnv: string,
): ModelProviderConfiguration {
  return {
    model: name,
    base_url: baseUrl,
    api_key_env: apiKeyEnv,
    enable_thinking: true,
    temperature: 0.1,
    max_tokens: 40000,
    timeout_sec: 600,
    max_retries: 3,
    max_concurrency: 10,
    input_price_yuan_per_million_tokens: 1,
    cached_input_price_yuan_per_million_tokens: 0.1,
    output_price_yuan_per_million_tokens: 2,
  }
}

const asr: AsrProviderConfiguration = {
  backend: 'bailian',
  api_key_env: 'DASHSCOPE_API_KEY',
  reuse: true,
  timeout_sec: 600,
  poll_interval_sec: 2,
  max_chars: 20,
  max_subtitle_duration_sec: 3.5,
}

const costSaving: ProviderBundle = {
  llm: model('deepseek-v4-flash', 'https://api.deepseek.com', 'DEEPSEEK_API_KEY'),
  vlm: model(
    'qwen3.7-plus',
    'https://dashscope.aliyuncs.com/compatible-mode/v1',
    'DASHSCOPE_API_KEY',
  ),
  asr,
}

const simple: ProviderBundle = {
  ...costSaving,
  llm: model(
    'qwen3.7-max',
    'https://dashscope.aliyuncs.com/compatible-mode/v1',
    'DASHSCOPE_API_KEY',
  ),
}

function settings(
  configured: Configured,
  profile: ProviderProfile = 'cost_saving',
): SettingsView {
  const providers = profile === 'simple' ? simple : costSaving
  return {
    values: {},
    base_path: '/workspace/config.toml',
    data_root: '/workspace/.cutmaster',
    secrets: {
      llm_configured: configured.llm,
      vlm_configured: configured.vlm,
      asr_configured: configured.asr,
    },
    connections: {
      profile,
      providers,
      presets: { cost_saving: costSaving, simple },
      credentials: {
        llm: credential(configured.llm),
        vlm: credential(configured.vlm),
        asr: credential(configured.asr),
      },
    },
  }
}

function credential(configured: boolean) {
  return configured
    ? {
        configured: true,
        suffix: 'SAFE',
        source: 'dotenv' as const,
        writable: true,
      }
    : {
        configured: false,
        suffix: null,
        source: 'none' as const,
        writable: true,
      }
}

function health(configured: Configured) {
  return jsonResponse({ status: 'ok', service: 'cutmaster', configured })
}

function jsonResponse(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      'Content-Type': status >= 400 ? 'application/problem+json' : 'application/json',
    },
  })
}

function client() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
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

describe('first Setup onboarding', () => {
  it('tests, saves, rechecks health, and returns to the exact gated deep link', async () => {
    let configured = false
    let savedBody: Record<string, unknown> | undefined
    const tested: Array<{ capability: string; body: Record<string, unknown> }> = []
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        if (url === '/api/health') {
          return health(
            configured
              ? { llm: true, vlm: true, asr: true }
              : { llm: false, vlm: false, asr: false },
          )
        }
        if (url === '/api/settings' && !init?.method) {
          return jsonResponse(settings({ llm: false, vlm: false, asr: false }))
        }
        const testMatch = url.match(/^\/api\/settings\/providers\/(llm|vlm|asr)\/test$/)
        if (testMatch && init?.method === 'POST') {
          tested.push({
            capability: testMatch[1],
            body: JSON.parse(String(init.body)) as Record<string, unknown>,
          })
          return jsonResponse({
            capability: testMatch[1],
            status: 'connected',
            latency_ms: 12.5,
          })
        }
        if (url === '/api/settings/providers' && init?.method === 'PUT') {
          savedBody = JSON.parse(String(init.body)) as Record<string, unknown>
          configured = true
          return jsonResponse({
            settings: settings({ llm: true, vlm: true, asr: true }, 'simple'),
            restart_required: true,
            credential_results: { llm: 'set', vlm: 'set', asr: 'set' },
          })
        }
        return jsonResponse({ detail: 'Not found' }, 404)
      }),
    )
    const router = createMemoryRouter(
      [
        { path: '/setup', element: <SetupPage /> },
        {
          path: '/',
          element: <ApplicationGate />,
          children: [
            {
              path: 'projects/:projectId/runs/:runId',
              element: <h1>Recovered deep link</h1>,
            },
          ],
        },
      ],
      {
        initialEntries: ['/projects/project_1/runs/run_9?panel=usage#attempt-2'],
      },
    )
    render(
      <QueryClientProvider client={client()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByRole('heading', { name: 'Setup required' })).toBeVisible()
    expect(router.state.location.pathname).toBe('/setup')
    expect(router.state.location.state).toEqual({
      returnTo: '/projects/project_1/runs/run_9?panel=usage#attempt-2',
    })

    const user = userEvent.setup()
    await user.click(await screen.findByRole('radio', { name: /Simple/ }))
    const secrets = screen.getAllByLabelText('API key')
    expect(secrets).toHaveLength(3)
    await user.type(secrets[0], 'one-shared-dashscope-key')

    for (const capability of ['LLM', 'VLM', 'ASR']) {
      const card = screen.getByRole('heading', { name: capability }).closest('article')
      if (!card) throw new Error(`${capability} card missing`)
      await user.click(within(card).getByRole('button', { name: 'Test connection' }))
      await waitFor(() =>
        expect(
          tested.some((item) => item.capability === capability.toLowerCase()),
        ).toBe(true),
      )
    }
    await user.click(screen.getByRole('button', { name: 'Save and check setup' }))

    expect(
      await screen.findByRole('heading', { name: 'Recovered deep link' }),
    ).toBeVisible()
    expect(router.state.location.pathname).toBe('/projects/project_1/runs/run_9')
    expect(router.state.location.search).toBe('?panel=usage')
    expect(router.state.location.hash).toBe('#attempt-2')
    expect(savedBody).toMatchObject({
      profile: 'simple',
      credentials: {
        llm: { action: 'set', value: 'one-shared-dashscope-key' },
        vlm: { action: 'set', value: 'one-shared-dashscope-key' },
        asr: { action: 'set', value: 'one-shared-dashscope-key' },
      },
    })
    expect(savedBody).not.toHaveProperty('providers')
    expect(tested).toHaveLength(3)
    expect(
      tested.every((item) => item.body.api_key === 'one-shared-dashscope-key'),
    ).toBe(true)
  })

  it('stays in Setup when saved credentials do not satisfy authoritative health', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        const url = String(input)
        if (url === '/api/health') {
          return health({ llm: false, vlm: true, asr: true })
        }
        if (url === '/api/settings' && !init?.method) {
          return jsonResponse(settings({ llm: false, vlm: true, asr: true }))
        }
        if (url === '/api/settings/providers' && init?.method === 'PUT') {
          return jsonResponse({
            settings: settings({ llm: true, vlm: true, asr: true }),
            restart_required: true,
            credential_results: { llm: 'set', vlm: 'kept', asr: 'kept' },
          })
        }
        return jsonResponse({ detail: 'Not found' }, 404)
      }),
    )
    const router = createMemoryRouter(
      [
        { path: '/setup', element: <SetupPage /> },
        { path: '/projects', element: <h1>Projects</h1> },
      ],
      { initialEntries: ['/setup'] },
    )
    render(
      <QueryClientProvider client={client()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    const user = userEvent.setup()
    await user.type(await screen.findByLabelText('API key'), 'new-deepseek-key')
    await user.click(screen.getByRole('button', { name: 'Save and check setup' }))

    expect(await screen.findByText(/Restart CutMaster/)).toBeVisible()
    expect(router.state.location.pathname).toBe('/setup')
    expect(screen.getByText('Missing connections: LLM')).toBeVisible()
    expect(screen.queryByRole('heading', { name: 'Projects' })).not.toBeInTheDocument()
  })

  it('renders Problem Details from a failed Settings read', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request) => {
        if (String(input) === '/api/health') {
          return health({ llm: false, vlm: false, asr: false })
        }
        return jsonResponse(
          {
            title: 'Settings unavailable',
            detail: 'The provider overlay cannot be read safely.',
            code: 'settings_unavailable',
          },
          503,
        )
      }),
    )
    const router = createMemoryRouter([{ path: '/setup', element: <SetupPage /> }], {
      initialEntries: ['/setup'],
    })
    render(
      <QueryClientProvider client={client()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(
      await screen.findByText(
        'The operation could not be completed. Check the current state and try again.',
      ),
    ).toBeVisible()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeVisible()
  })

  it('rejects external and looping return targets', async () => {
    expect(setupReturnTarget({ returnTo: '/projects/p1/runs/r2?x=1#usage' })).toBe(
      '/projects/p1/runs/r2?x=1#usage',
    )
    for (const returnTo of [
      'https://evil.example',
      '//evil.example/path',
      '/\\evil.example/path',
      '/setup',
    ]) {
      expect(setupReturnTarget({ returnTo })).toBe('/projects')
    }

    vi.stubGlobal(
      'fetch',
      vi.fn(async () => health({ llm: true, vlm: true, asr: true })),
    )
    const router = createMemoryRouter(
      [
        { path: '/setup', element: <SetupPage /> },
        { path: '/projects', element: <h1>Projects</h1> },
      ],
      {
        initialEntries: [
          {
            pathname: '/setup',
            state: { returnTo: 'https://evil.example/phish' },
          },
        ],
      },
    )
    render(
      <QueryClientProvider client={client()}>
        <RouterProvider router={router} />
      </QueryClientProvider>,
    )

    expect(await screen.findByRole('heading', { name: 'Projects' })).toBeVisible()
    expect(router.state.location.pathname).toBe('/projects')
  })
})
