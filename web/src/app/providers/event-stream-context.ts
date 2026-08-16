import { createContext, useContext } from 'react'

export type EventStreamConnectionState =
  'connecting' | 'connected' | 'disconnected' | 'unsupported'

export interface EventStreamContextValue {
  connectionState: EventStreamConnectionState
  isConnected: boolean
  pollingFallback: boolean
}

const fallbackContext: EventStreamContextValue = {
  connectionState: 'unsupported',
  isConnected: false,
  pollingFallback: true,
}

export const EventStreamContext =
  createContext<EventStreamContextValue>(fallbackContext)

export function useEventStream() {
  return useContext(EventStreamContext)
}
