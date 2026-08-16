import { type PropsWithChildren } from 'react'

import { ColorModeProvider } from '@/app/providers/ColorModeProvider'
import { EventStreamProvider } from '@/app/providers/EventStreamProvider'
import { LocaleProvider } from '@/app/providers/LocaleProvider'
import { QueryProvider } from '@/app/providers/QueryProvider'

export function AppProviders({ children }: PropsWithChildren) {
  return (
    <ColorModeProvider>
      <LocaleProvider>
        <QueryProvider>
          <EventStreamProvider>{children}</EventStreamProvider>
        </QueryProvider>
      </LocaleProvider>
    </ColorModeProvider>
  )
}
