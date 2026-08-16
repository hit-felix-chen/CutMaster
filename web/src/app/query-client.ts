import { QueryClient } from '@tanstack/react-query'

function httpStatus(error: unknown): number | null {
  if (typeof error !== 'object' || error === null || !('status' in error)) {
    return null
  }
  return typeof error.status === 'number' ? error.status : null
}

export function createAppQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        refetchOnWindowFocus: false,
        retry: (failureCount, error) => {
          const status = httpStatus(error)
          if (status !== null && status >= 400 && status < 500) {
            return false
          }
          return failureCount < 2
        },
        staleTime: 15_000,
      },
      mutations: {
        retry: false,
      },
    },
  })
}
