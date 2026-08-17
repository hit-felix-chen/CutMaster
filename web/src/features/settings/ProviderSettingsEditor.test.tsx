import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ProviderSettingsEditor } from '@/features/settings/ProviderSettingsEditor'
import type {
  AsrProviderConfiguration,
  ModelProviderConfiguration,
  ProviderBundle,
  SettingsView,
} from '@/features/shared/api'
import i18n from '@/i18n'

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
  options: { processLlm?: boolean; profile?: 'cost_saving' | 'simple' } = {},
): SettingsView {
  const profile = options.profile ?? 'cost_saving'
  const providers = profile === 'simple' ? simple : costSaving
  return {
    values: {},
    base_path: '/workspace/config.toml',
    data_root: '/workspace/.cutmaster',
    secrets: {
      llm_configured: options.processLlm ?? false,
      vlm_configured: true,
      asr_configured: true,
    },
    connections: {
      profile,
      providers,
      presets: { cost_saving: costSaving, simple },
      credentials: {
        llm: options.processLlm
          ? {
              configured: true,
              suffix: 'PROC',
              source: 'process' as const,
              writable: false,
            }
          : {
              configured: false,
              suffix: null,
              source: 'none' as const,
              writable: true,
            },
        vlm: {
          configured: true,
          suffix: 'DASH',
          source: 'dotenv' as const,
          writable: true,
        },
        asr: {
          configured: true,
          suffix: 'DASH',
          source: 'dotenv' as const,
          writable: true,
        },
      },
    },
  }
}

function response(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function renderEditor(value = settings(), setupMode = false) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={client}>
      <ProviderSettingsEditor settings={value} setupMode={setupMode} />
    </QueryClientProvider>,
  )
}

beforeEach(async () => {
  await i18n.changeLanguage('en-US')
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('Provider Settings editor', () => {
  it('keeps disabled actions in the connection header and restores the saved draft', async () => {
    renderEditor()
    const save = screen.getByRole('button', { name: 'Save' })
    const undo = screen.getByRole('button', { name: 'Undo changes' })
    const heading = screen.getByRole('heading', { name: 'Model connections' })
    const header = heading.closest('header')

    expect(header).toContainElement(undo)
    expect(header).toContainElement(save)
    expect(undo).toBeDisabled()
    expect(save).toBeDisabled()
    expect(document.querySelector('.settings-save-row')).not.toBeInTheDocument()

    const modelInput = screen.getByDisplayValue('deepseek-v4-flash')
    await userEvent.clear(modelInput)
    await userEvent.type(modelInput, 'temporary-model')
    expect(undo).toBeEnabled()
    expect(save).toBeEnabled()

    await userEvent.click(undo)
    expect(screen.getByDisplayValue('deepseek-v4-flash')).toBeVisible()
    expect(screen.getByRole('radio', { name: /Cost Saving/ })).toBeChecked()
    expect(undo).toBeDisabled()
    expect(save).toBeDisabled()
  })

  it('uses the server preset and propagates one shared DashScope credential', async () => {
    let savedBody: Record<string, unknown> | undefined
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        if (String(input) === '/api/settings/providers') {
          savedBody = JSON.parse(String(init?.body)) as Record<string, unknown>
          return response({
            settings: settings({ profile: 'simple' }),
            restart_required: true,
            credential_results: { llm: 'set', vlm: 'set', asr: 'set' },
          })
        }
        return response({}, 404)
      }),
    )
    renderEditor()

    await userEvent.click(screen.getByRole('radio', { name: /Simple/ }))
    await userEvent.selectOptions(screen.getByLabelText('LLM credential action'), 'set')
    const secrets = screen.getAllByLabelText('API key')
    expect(secrets).toHaveLength(3)
    await userEvent.type(secrets[0], 'shared-secret')
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(savedBody).toBeDefined())
    expect(savedBody?.profile).toBe('simple')
    expect(savedBody).not.toHaveProperty('providers')
    expect(savedBody?.credentials).toEqual({
      llm: { action: 'set', value: 'shared-secret' },
      vlm: { action: 'set', value: 'shared-secret' },
      asr: { action: 'set', value: 'shared-secret' },
    })
    expect(await screen.findByText(/Restart CutMaster/)).toBeVisible()
    expect(screen.queryByDisplayValue('shared-secret')).not.toBeInTheDocument()
  })

  it('switches to Custom when a field changes and sends every provider', async () => {
    let savedBody: Record<string, unknown> | undefined
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_input: string | URL | Request, init?: RequestInit) => {
        savedBody = JSON.parse(String(init?.body)) as Record<string, unknown>
        const custom = settings()
        custom.connections.profile = 'custom'
        custom.connections.providers = savedBody.providers as ProviderBundle
        return response({
          settings: custom,
          restart_required: true,
          credential_results: { llm: 'kept', vlm: 'kept', asr: 'kept' },
        })
      }),
    )
    renderEditor()

    const modelInput = screen.getByDisplayValue('deepseek-v4-flash')
    await userEvent.clear(modelInput)
    await userEvent.type(modelInput, 'deepseek-custom')
    expect(screen.getByRole('radio', { name: /Custom/ })).toBeChecked()
    await userEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(savedBody).toBeDefined())
    expect(savedBody?.profile).toBe('custom')
    expect(savedBody?.providers).toMatchObject({
      llm: { model: 'deepseek-custom' },
      vlm: { model: 'qwen3.7-plus' },
      asr: { backend: 'bailian' },
    })
  })

  it('locks a process credential while preserving provider edits', () => {
    renderEditor(settings({ processLlm: true }))

    expect(screen.getByText(/Managed by the process environment/)).toBeVisible()
    expect(screen.queryByLabelText('LLM credential action')).not.toBeInTheDocument()
    expect(screen.getByDisplayValue('deepseek-v4-flash')).toBeEnabled()
  })

  it('preserves process credential locking during first-time Setup', () => {
    renderEditor(settings({ processLlm: true }), true)

    expect(screen.getByText(/Managed by the process environment/)).toBeVisible()
    expect(screen.queryByLabelText('LLM credential action')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save and check setup' })).toBeDisabled()
  })

  it('tests the current candidate with an ephemeral key and displays real latency', async () => {
    let testBody: Record<string, unknown> | undefined
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
        if (String(input) === '/api/settings/providers/llm/test') {
          testBody = JSON.parse(String(init?.body)) as Record<string, unknown>
          return response({ capability: 'llm', status: 'connected', latency_ms: 14.2 })
        }
        return response({}, 404)
      }),
    )
    renderEditor()

    await userEvent.selectOptions(screen.getByLabelText('LLM credential action'), 'set')
    await userEvent.type(screen.getByLabelText('API key'), 'ephemeral-secret')
    const llmCard = screen.getByRole('heading', { name: 'LLM' }).closest('article')
    if (!llmCard) throw new Error('LLM card missing')
    await userEvent.click(
      Array.from(llmCard.querySelectorAll('button')).find((button) =>
        button.textContent?.includes('Test connection'),
      ) as HTMLButtonElement,
    )

    await waitFor(() => expect(testBody).toBeDefined())
    expect(testBody?.api_key).toBe('ephemeral-secret')
    expect(testBody?.configuration).toMatchObject({ model: 'deepseek-v4-flash' })
    expect(await screen.findByText('Connected · 14.2 ms')).toBeVisible()
  })
})
