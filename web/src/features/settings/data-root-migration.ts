import { useQuery } from '@tanstack/react-query'

import {
  api,
  type DataRootMigration,
  type DataRootMigrationStatus,
} from '@/features/shared/api'

const activeStatuses = new Set<DataRootMigrationStatus>([
  'requested',
  'quiescing',
  'copying',
  'verifying',
  'cancelling',
  'rolling_back',
  'switching',
])

export function isDataRootMigrationActive(
  migration: DataRootMigration | null | undefined,
) {
  return Boolean(migration && activeStatuses.has(migration.status))
}

export function isDataRootMigrationBlocking(
  migration: DataRootMigration | null | undefined,
) {
  return Boolean(
    migration &&
    (activeStatuses.has(migration.status) || migration.status === 'restart_required'),
  )
}

export function useCurrentDataRootMigration() {
  return useQuery({
    queryKey: ['settings-storage-migration-current'],
    queryFn: api.settings.currentDataRootMigration,
    refetchInterval: (query) =>
      isDataRootMigrationBlocking(query.state.data?.migration) ? 1000 : false,
  })
}
