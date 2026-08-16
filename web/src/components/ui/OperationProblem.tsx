import { useTranslation } from 'react-i18next'

import { ApiError } from '@/features/shared/api'
import {
  problemBlockerMessage,
  problemFieldMessage,
  problemMessage,
} from '@/i18n/problem-messages'

export function OperationProblem({ error }: { error: unknown }) {
  const { t } = useTranslation('common')
  const problem = error instanceof ApiError ? error.problem : null
  const blockers = Array.isArray(problem?.blockers) ? problem.blockers : []
  const fieldErrors = Array.isArray(problem?.field_errors) ? problem.field_errors : []
  return (
    <div className="operation-problem" role="alert">
      <p>{problem ? problemMessage(t, problem) : t('common.errorTitle')}</p>
      {fieldErrors.length ? (
        <ul>
          {fieldErrors.map((issue, index) => (
            <li key={`field-${index}`}>{problemFieldMessage(t, issue)}</li>
          ))}
        </ul>
      ) : null}
      {blockers.length ? (
        <ul>
          {blockers.map((blocker, index) => (
            <li key={`blocker-${index}`}>{problemBlockerMessage(t, blocker)}</li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}
