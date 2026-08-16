import { useQuery } from '@tanstack/react-query'

import { api } from '@/features/shared/api'

export function useApplicationHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: api.health.get,
  })
}
