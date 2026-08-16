import { useQuery } from '@tanstack/react-query'

import { api } from '@/features/shared/api'

export function useApplicationHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: api.health.get,
    refetchInterval: (query) => {
      const root = query.state.data?.data_root
      return root?.maintenance || root?.restart_required ? 1000 : false
    },
  })
}
