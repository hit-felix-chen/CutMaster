import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { EventStreamProvider } from '@/app/providers/EventStreamProvider'
import { useEventStream } from '@/app/providers/event-stream-context'
import { parseDurableEvent } from '@/app/providers/event-stream'

type Listener = EventListenerOrEventListenerObject

class FakeEventSource {
  static instances: FakeEventSource[] = []

  readonly url: string
  readonly close = vi.fn()
  private readonly listeners = new Map<string, Set<Listener>>()

  constructor(url: string | URL) {
    this.url = String(url)
    FakeEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: Listener) {
    const values = this.listeners.get(type) ?? new Set()
    values.add(listener)
    this.listeners.set(type, values)
  }

  removeEventListener(type: string, listener: Listener) {
    this.listeners.get(type)?.delete(listener)
  }

  emit(type: string, data?: string, lastEventId = '') {
    const event =
      data === undefined
        ? new Event(type)
        : new MessageEvent(type, { data, lastEventId })
    for (const listener of this.listeners.get(type) ?? []) {
      if (typeof listener === 'function') listener(event)
      else listener.handleEvent(event)
    }
  }

  listenerCount() {
    return [...this.listeners.values()].reduce(
      (count, values) => count + values.size,
      0,
    )
  }
}

function queryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false },
      mutations: { retry: false },
    },
  })
}

function Probe() {
  const stream = useEventStream()
  return (
    <output>
      {stream.connectionState}:{stream.pollingFallback ? 'polling' : 'stream'}
    </output>
  )
}

function eventData(overrides: Record<string, unknown> = {}) {
  return JSON.stringify({
    event_id: 7,
    event_type: 'attempt.running',
    occurred_at: '2026-08-16T00:00:00Z',
    object_type: 'material',
    object_id: 'material_1',
    command_id: null,
    attempt_id: 'attempt_1',
    job_id: 'job_1',
    payload: {},
    schema_version: '1.0',
    ...overrides,
  })
}

function renderProvider(client = queryClient()) {
  const view = render(
    <QueryClientProvider client={client}>
      <EventStreamProvider>
        <Probe />
      </EventStreamProvider>
    </QueryClientProvider>,
  )
  return { client, ...view }
}

beforeEach(() => {
  FakeEventSource.instances = []
  window.sessionStorage.clear()
  vi.stubGlobal('EventSource', FakeEventSource)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('EventStreamProvider', () => {
  it('uses one native EventSource and lets it reconnect with polling fallback', () => {
    renderProvider()
    expect(FakeEventSource.instances).toHaveLength(1)
    const source = FakeEventSource.instances[0]
    expect(source.url).toBe('/api/events/stream')
    expect(screen.getByText('connecting:polling')).toBeInTheDocument()

    act(() => source.emit('open'))
    expect(screen.getByText('connected:stream')).toBeInTheDocument()

    act(() => source.emit('error'))
    expect(screen.getByText('disconnected:polling')).toBeInTheDocument()

    act(() => source.emit('open'))
    expect(screen.getByText('connected:stream')).toBeInTheDocument()
    expect(FakeEventSource.instances).toHaveLength(1)
  })

  it('resumes a page refresh from the session cursor without replacing native reconnect', () => {
    window.sessionStorage.setItem('cutmaster:event-stream:last-event-id', '6')
    const first = renderProvider()
    const source = FakeEventSource.instances[0]
    expect(source.url).toBe('/api/events/stream?after_event_id=6')

    act(() => source.emit('durable_event', eventData({ event_id: 7 }), '7'))
    expect(window.sessionStorage.getItem('cutmaster:event-stream:last-event-id')).toBe(
      '7',
    )
    expect(FakeEventSource.instances).toHaveLength(1)

    first.unmount()
    renderProvider()
    expect(FakeEventSource.instances[1].url).toBe('/api/events/stream?after_event_id=7')
  })

  it('invalidates only the material domain, exact detail, matching workspace, and activity', async () => {
    const client = queryClient()
    client.setQueryData(['activity'], [])
    client.setQueryData(['materials', 'video', '', 'name_asc'], [])
    client.setQueryData(['material', 'material_1'], { material_id: 'material_1' })
    client.setQueryData(['material', 'material_2'], { material_id: 'material_2' })
    client.setQueryData(['material-memory', 'material_1', 'timeline'], {})
    client.setQueryData(['project-workspace', 'project_1'], {
      materials: { video: [{ material_id: 'material_1' }] },
    })
    client.setQueryData(['project-workspace', 'project_2'], {
      materials: { video: [{ material_id: 'material_2' }] },
    })
    client.setQueryData(['settings'], {})
    renderProvider(client)

    act(() => FakeEventSource.instances[0].emit('durable_event', eventData()))

    await waitFor(() => {
      expect(client.getQueryState(['activity'])?.isInvalidated).toBe(true)
    })
    expect(
      client.getQueryState(['materials', 'video', '', 'name_asc'])?.isInvalidated,
    ).toBe(true)
    expect(client.getQueryState(['material', 'material_1'])?.isInvalidated).toBe(true)
    expect(
      client.getQueryState(['material-memory', 'material_1', 'timeline'])
        ?.isInvalidated,
    ).toBe(true)
    expect(
      client.getQueryState(['project-workspace', 'project_1'])?.isInvalidated,
    ).toBe(true)
    expect(client.getQueryState(['material', 'material_2'])?.isInvalidated).toBe(false)
    expect(
      client.getQueryState(['project-workspace', 'project_2'])?.isInvalidated,
    ).toBe(false)
    expect(client.getQueryState(['settings'])?.isInvalidated).toBe(false)
  })

  it('invalidates every authoritative REST query on a valid resync request', async () => {
    const client = queryClient()
    client.setQueryData(['materials', 'video'], [])
    client.setQueryData(['projects', '', 'updated_desc'], [])
    client.setQueryData(['settings'], {})
    client.setQueryData(['local-draft', 'project_1'], { dirty: true })
    renderProvider(client)

    act(() =>
      FakeEventSource.instances[0].emit(
        'resync_required',
        JSON.stringify({
          schema_version: '1.0',
          reason: 'cursor_expired',
          requested_after_event_id: 2,
          available_first_event_id: 8,
          available_last_event_id: 10,
          action: 'refetch_authoritative_state',
        }),
        '10',
      ),
    )

    await waitFor(() => {
      expect(client.getQueryState(['materials', 'video'])?.isInvalidated).toBe(true)
    })
    expect(client.getQueryState(['projects', '', 'updated_desc'])?.isInvalidated).toBe(
      true,
    )
    expect(client.getQueryState(['settings'])?.isInvalidated).toBe(true)
    expect(client.getQueryState(['local-draft', 'project_1'])?.isInvalidated).toBe(
      false,
    )
    expect(window.sessionStorage.getItem('cutmaster:event-stream:last-event-id')).toBe(
      '10',
    )
  })

  it('uses migration events only as an invalidation hint for authoritative REST state', async () => {
    const client = queryClient()
    client.setQueryData(['settings-storage-migration-current'], {
      migration: null,
    })
    client.setQueryData(['settings-storage'], {})
    client.setQueryData(['settings'], {})
    client.setQueryData(['health'], {})
    client.setQueryData(['materials', 'video'], [])
    renderProvider(client)

    act(() =>
      FakeEventSource.instances[0].emit(
        'durable_event',
        eventData({
          event_type: 'settings.data_root_migration.updated',
          object_type: 'data_root_migration',
          object_id: 'drm_1',
          attempt_id: null,
          job_id: null,
        }),
      ),
    )

    await waitFor(() =>
      expect(
        client.getQueryState(['settings-storage-migration-current'])?.isInvalidated,
      ).toBe(true),
    )
    expect(client.getQueryState(['settings-storage'])?.isInvalidated).toBe(true)
    expect(client.getQueryState(['settings'])?.isInvalidated).toBe(true)
    expect(client.getQueryState(['health'])?.isInvalidated).toBe(true)
    expect(client.getQueryState(['materials', 'video'])?.isInvalidated).toBe(false)
  })

  it('invalidates run collections when a new frozen edit is not cached yet', async () => {
    const client = queryClient()
    client.setQueryData(['run', 'run_1'], { frozen_edits: [] })
    client.setQueryData(['project-runs', 'project_1'], { items: [] })
    client.setQueryData(['project-workspace', 'project_1'], { project: {} })
    client.setQueryData(['frozen-edit-review', 'other_edit'], {})
    client.setQueryData(['settings'], {})
    renderProvider(client)

    act(() =>
      FakeEventSource.instances[0].emit(
        'durable_event',
        eventData({
          event_type: 'frozen_edit.created',
          object_type: 'frozen_edit',
          object_id: 'new_edit',
        }),
      ),
    )

    await waitFor(() => {
      expect(client.getQueryState(['run', 'run_1'])?.isInvalidated).toBe(true)
    })
    expect(client.getQueryState(['project-runs', 'project_1'])?.isInvalidated).toBe(
      true,
    )
    expect(
      client.getQueryState(['project-workspace', 'project_1'])?.isInvalidated,
    ).toBe(true)
    expect(
      client.getQueryState(['frozen-edit-review', 'other_edit'])?.isInvalidated,
    ).toBe(true)
    expect(client.getQueryState(['settings'])?.isInvalidated).toBe(false)
  })

  it('ignores malformed, invalid, and non-public event data', () => {
    const client = queryClient()
    const invalidate = vi.spyOn(client, 'invalidateQueries')
    renderProvider(client)
    const source = FakeEventSource.instances[0]
    act(() => source.emit('open'))

    act(() => {
      source.emit('durable_event', '{bad json')
      source.emit('durable_event', eventData({ object_id: '' }))
      source.emit('durable_event', eventData({ payload: { private: true } }))
      source.emit('resync_required', JSON.stringify({ action: 'something_else' }))
    })

    expect(invalidate).not.toHaveBeenCalled()
    expect(screen.getByText('connected:stream')).toBeInTheDocument()
  })

  it('closes the stream and removes listeners on cleanup', () => {
    const { unmount } = renderProvider()
    const source = FakeEventSource.instances[0]
    expect(source.listenerCount()).toBe(4)

    unmount()

    expect(source.close).toHaveBeenCalledOnce()
    expect(source.listenerCount()).toBe(0)
  })

  it('uses polling when EventSource is unsupported', () => {
    vi.stubGlobal('EventSource', undefined)
    renderProvider()
    expect(screen.getByText('unsupported:polling')).toBeInTheDocument()
    expect(FakeEventSource.instances).toHaveLength(0)
  })
})

describe('parseDurableEvent', () => {
  it('accepts only the public versioned durable event shape', () => {
    expect(parseDurableEvent(eventData())?.object_id).toBe('material_1')
    expect(parseDurableEvent(eventData({ event_id: true }))).toBeNull()
    expect(parseDurableEvent(eventData({ schema_version: '2.0' }))).toBeNull()
  })
})
