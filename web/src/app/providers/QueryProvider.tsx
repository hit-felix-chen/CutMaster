import { QueryClientProvider } from '@tanstack/react-query'
import { useState, type PropsWithChildren } from 'react'

import { createAppQueryClient } from '@/app/query-client'

export function QueryProvider({ children }: PropsWithChildren) {
  const [queryClient] = useState(createAppQueryClient)

  return <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
}
