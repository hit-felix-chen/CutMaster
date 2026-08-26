import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ExecutionLogAccess } from '@/features/shared/LiveLogDialog'
import i18n from '@/i18n'

class FakeEventSource {
  static instances: FakeEventSource[] = []

  readonly url: string
  closed = false
  private readonly listeners = new Map<string, EventListener[]>()

  constructor(url: string | URL) {
    this.url = String(url)
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: EventListener) {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener])
  }

  close() {
    this.closed = true
  }

  emit(type: string, data: object) {
    const event = new MessageEvent(type, { data: JSON.stringify(data) })
    for (const listener of this.listeners.get(type) ?? []) listener(event)
  }
}

const execution = {
  attempt: {
    attempt_id: 'attempt_1',
    operation_type: 'aster_planning',
    owner_type: 'run',
    owner_id: 'run_1',
    sequence: 1,
    status: 'running',
    created_at: '2026-08-17T00:00:00Z',
    updated_at: '2026-08-17T00:00:00Z',
  },
  job: {
    job_id: 'job_1',
    attempt_id: 'attempt_1',
    status: 'running',
    stop_requested: false,
    progress: {},
    created_at: '2026-08-17T00:00:00Z',
    updated_at: '2026-08-17T00:00:00Z',
  },
  log: {
    path: '/Users/example/.cutmaster/logs/jobs/job_1.log',
    exists: true,
  },
}

beforeEach(async () => {
  await i18n.changeLanguage('en-US')
  FakeEventSource.instances = []
  vi.stubGlobal('EventSource', FakeEventSource)
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const full = String(input).endsWith('/logs/full')
      const entries = [
        ...(full
          ? [
              {
                cursor: 60,
                timestamp: '2026-08-17 09:59:59.000+08:00',
                level: 'DEBUG',
                component: 'analyser',
                event: 'stage.progress',
                fields: 'stage=analysis',
                message: 'Historical analysis line',
              },
            ]
          : []),
        {
          cursor: 120,
          timestamp: '2026-08-17 10:00:00.000+08:00',
          level: 'INFO',
          component: 'planners',
          event: 'stage.start',
          fields: 'stage=planning',
          message: 'Planning started',
        },
      ]
      return Promise.resolve(
        new Response(
          JSON.stringify({
            attempt_id: 'attempt_1',
            log: execution.log,
            entries,
            start_cursor: 0,
            end_cursor: 120,
            has_more_before: !full,
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } },
        ),
      )
    }),
  )
})

afterEach(() => vi.unstubAllGlobals())

describe('Attempt live logs', () => {
  it('shows the absolute path, streams coloured lines, and disconnects on close', async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={client}>
        <ExecutionLogAccess execution={execution} />
      </QueryClientProvider>,
    )

    expect(screen.getByText(execution.log.path)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'View live logs' }))
    expect(await screen.findByText(/Planning started/)).toBeInTheDocument()
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    const source = FakeEventSource.instances[0]
    expect(source.url).toContain('after_cursor=120')

    act(() => {
      source.emit('log_entry', {
        cursor: 180,
        timestamp: '2026-08-17 10:00:01.000+08:00',
        level: 'ERROR',
        component: 'model',
        event: 'model.fail',
        fields: '',
        message: 'Model request failed',
      })
    })
    const streamed = await screen.findByText('Model request failed')
    expect(streamed.closest('.execution-log-line')).toHaveClass(
      'execution-log-line--error',
    )

    await userEvent.click(screen.getByRole('button', { name: 'Close' }))
    expect(source.closed).toBe(true)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('closes from the empty backdrop and disconnects the live stream', async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={client}>
        <ExecutionLogAccess execution={execution} />
      </QueryClientProvider>,
    )

    await userEvent.click(screen.getByRole('button', { name: 'View live logs' }))
    await screen.findByRole('dialog')
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))
    const source = FakeEventSource.instances[0]
    const layer = screen.getByRole('dialog').parentElement
    expect(layer).not.toBeNull()

    await userEvent.click(layer!)

    expect(source.closed).toBe(true)
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('loads the full snapshot, keeps streaming, and filters by level and task', async () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    })
    render(
      <QueryClientProvider client={client}>
        <ExecutionLogAccess execution={execution} />
      </QueryClientProvider>,
    )

    await userEvent.click(screen.getByRole('button', { name: 'View live logs' }))
    expect(await screen.findByText(/Planning started/)).toBeInTheDocument()
    expect(screen.queryByText(/Historical analysis line/)).not.toBeInTheDocument()
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1))

    await userEvent.click(screen.getByRole('button', { name: 'Read full log' }))
    expect(await screen.findByText(/Historical analysis line/)).toBeInTheDocument()
    await waitFor(() => expect(FakeEventSource.instances).toHaveLength(2))
    expect(FakeEventSource.instances[0].closed).toBe(true)
    expect(FakeEventSource.instances[1].url).toContain('after_cursor=120')

    act(() => {
      FakeEventSource.instances[1].emit('log_entry', {
        cursor: 180,
        timestamp: '2026-08-17 10:00:01.000+08:00',
        level: 'ERROR',
        component: 'model',
        event: 'model.fail',
        fields: '',
        message: 'Model failed',
      })
      FakeEventSource.instances[1].emit('log_entry', {
        cursor: 220,
        timestamp: '2026-08-17 10:00:02.000+08:00',
        level: 'WARNING',
        component: 'analyser',
        event: 'fallback.apply',
        fields: '',
        message: 'Analysis fallback',
      })
    })

    await userEvent.selectOptions(screen.getByLabelText('Log level'), 'ERROR')
    expect(screen.getByText('Model failed')).toBeInTheDocument()
    expect(screen.queryByText(/Planning started/)).not.toBeInTheDocument()
    expect(screen.queryByText('Analysis fallback')).not.toBeInTheDocument()

    await userEvent.selectOptions(screen.getByLabelText('Log level'), '__all__')
    await userEvent.selectOptions(screen.getByLabelText('Task'), 'analyser')
    expect(screen.getByText(/Historical analysis line/)).toBeInTheDocument()
    expect(screen.getByText('Analysis fallback')).toBeInTheDocument()
    expect(screen.queryByText('Model failed')).not.toBeInTheDocument()
  })
})
