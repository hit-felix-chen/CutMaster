import { Navigate } from 'react-router-dom'

import { useApplicationHealth } from '@/app/use-application-health'
import { AppShell } from '@/components/layout/AppShell'
import { ErrorState, LoadingState } from '@/components/ui/AsyncState'

export function ApplicationGate() {
  const health = useApplicationHealth()

  if (health.isPending) {
    return (
      <main className="standalone-page">
        <LoadingState />
      </main>
    )
  }
  if (health.isError) {
    return (
      <main className="standalone-page">
        <ErrorState onRetry={() => void health.refetch()} />
      </main>
    )
  }
  if (Object.values(health.data.configured).some((configured) => !configured)) {
    return <Navigate to="/setup" replace />
  }
  return <AppShell />
}
