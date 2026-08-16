import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useMemo, useState, type PropsWithChildren } from 'react'
import { useTranslation } from 'react-i18next'

import {
  EventStreamContext,
  type EventStreamConnectionState,
  type EventStreamContextValue,
  useEventStream,
} from '@/app/providers/event-stream-context'
import {
  invalidateAuthoritativeState,
  invalidateDurableEvent,
  parseDurableEvent,
  parseResyncRequest,
} from '@/app/providers/event-stream'

const cursorStorageKey = 'cutmaster:event-stream:last-event-id'

function storedCursor(): number | null {
  try {
    const value = window.sessionStorage.getItem(cursorStorageKey)
    if (value === null || !/^\d+$/.test(value)) return null
    const parsed = Number(value)
    return Number.isSafeInteger(parsed) ? parsed : null
  } catch {
    return null
  }
}

function streamUrl(): string {
  const cursor = storedCursor()
  return cursor === null
    ? '/api/events/stream'
    : `/api/events/stream?after_event_id=${cursor}`
}

function numericCursor(value: unknown): number | null {
  if (typeof value === 'number') {
    return Number.isSafeInteger(value) && value >= 0 ? value : null
  }
  if (typeof value !== 'string' || !/^\d+$/.test(value)) return null
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) ? parsed : null
}

function rememberCursor(message: MessageEvent<string>, fallback: unknown) {
  const cursor = numericCursor(message.lastEventId) ?? numericCursor(fallback)
  if (cursor === null) return
  try {
    window.sessionStorage.setItem(cursorStorageKey, String(cursor))
  } catch {
    // Session storage can be unavailable in privacy modes; native reconnect still works.
  }
}

export function EventStreamProvider({ children }: PropsWithChildren) {
  const queryClient = useQueryClient()
  const eventSourceSupported = typeof EventSource !== 'undefined'
  const [connectionState, setConnectionState] = useState<EventStreamConnectionState>(
    () => (eventSourceSupported ? 'connecting' : 'unsupported'),
  )

  useEffect(() => {
    if (!eventSourceSupported) return

    const source = new EventSource(streamUrl())
    const onOpen = () => setConnectionState('connected')
    const onError = () => setConnectionState('disconnected')
    const onDurableEvent = (message: MessageEvent<string>) => {
      const event = parseDurableEvent(message.data)
      if (!event) return
      rememberCursor(message, event.event_id)
      void invalidateDurableEvent(queryClient, event)
    }
    const onResyncRequired = (message: MessageEvent<string>) => {
      const request = parseResyncRequest(message.data)
      if (!request) return
      rememberCursor(message, request.available_last_event_id)
      void invalidateAuthoritativeState(queryClient)
    }

    source.addEventListener('open', onOpen)
    source.addEventListener('error', onError)
    source.addEventListener('durable_event', onDurableEvent as EventListener)
    source.addEventListener('resync_required', onResyncRequired as EventListener)
    return () => {
      source.removeEventListener('open', onOpen)
      source.removeEventListener('error', onError)
      source.removeEventListener('durable_event', onDurableEvent as EventListener)
      source.removeEventListener('resync_required', onResyncRequired as EventListener)
      source.close()
    }
  }, [eventSourceSupported, queryClient])

  const value = useMemo<EventStreamContextValue>(
    () => ({
      connectionState,
      isConnected: connectionState === 'connected',
      pollingFallback: connectionState !== 'connected',
    }),
    [connectionState],
  )
  return (
    <EventStreamContext.Provider value={value}>{children}</EventStreamContext.Provider>
  )
}

export function EventStreamStatus() {
  const { t } = useTranslation('common')
  const { connectionState } = useEventStream()
  return (
    <div
      className={`event-stream-status event-stream-status--${connectionState}`}
      role="status"
      aria-live="polite"
      title={t(`realtime.${connectionState}Detail`)}
    >
      <span aria-hidden="true" />
      {t(`realtime.${connectionState}`)}
    </div>
  )
}
