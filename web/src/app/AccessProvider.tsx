import { useQuery } from '@tanstack/react-query'
import type { PropsWithChildren } from 'react'

import { AccessContext } from '@/app/access-context'
import { ErrorState, LoadingState } from '@/components/ui/AsyncState'
import { apiRequest } from '@/features/shared/api'

export function AccessProvider({ children }: PropsWithChildren) {
  const access = useQuery({
    queryKey: ['access'],
    queryFn: () => apiRequest<{ can_write: boolean }>('/api/access'),
    staleTime: 0,
  })
  if (access.isPending) return <LoadingState />
  if (access.isError) return <ErrorState onRetry={() => void access.refetch()} />
  return (
    <AccessContext value={access.data.can_write === true}>{children}</AccessContext>
  )
}
