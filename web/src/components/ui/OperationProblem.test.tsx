import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { OperationProblem } from '@/components/ui/OperationProblem'
import { ApiError } from '@/features/shared/api'
import i18n from '@/i18n'

afterEach(async () => {
  await i18n.changeLanguage('en-US')
})

describe('OperationProblem', () => {
  it('localises a Problem code and never renders the backend detail', () => {
    render(
      <OperationProblem
        error={
          new ApiError(409, {
            code: 'material_inconsistent',
            detail: '/private/path/source.mp4 has secret provider payload',
          })
        }
      />,
    )

    expect(
      screen.getByText('The managed source no longer matches its import record.'),
    ).toBeVisible()
    expect(screen.queryByText(/private\/path/)).not.toBeInTheDocument()
  })

  it('localises structured blockers and validation fields in Chinese', async () => {
    await i18n.changeLanguage('zh-CN')
    render(
      <OperationProblem
        error={
          new ApiError(409, {
            code: 'material_referenced',
            blockers: [
              {
                kind: 'run_snapshot',
                project_name: 'lalaland',
                run_sequence: 1,
                navigation: {
                  kind: 'run',
                  project_id: 'project_123',
                  run_id: 'run_123',
                },
              },
            ],
            field_errors: [{ field: 'body.target_duration_sec' }],
          })
        }
      />,
    )

    expect(
      screen.getByText('该素材仍被项目或剪辑运行引用，暂时不能删除。'),
    ).toBeVisible()
    expect(screen.getByText('lalaland · 剪辑运行 1 快照仍在使用该素材。')).toBeVisible()
    expect(screen.queryByText(/project_123|run_123/)).not.toBeInTheDocument()
    expect(screen.getByText('字段 body.target_duration_sec 无效。')).toBeVisible()
  })

  it('uses a safe generic message for an unknown code', () => {
    render(
      <OperationProblem
        error={new ApiError(409, { code: 'new_server_code', detail: 'raw detail' })}
      />,
    )
    expect(
      screen.getByText(
        'The operation could not be completed. Check the current state and try again.',
      ),
    ).toBeVisible()
    expect(screen.queryByText('raw detail')).not.toBeInTheDocument()
  })

  it('localises Data Root blocker kinds and nested metadata', () => {
    render(
      <OperationProblem
        error={
          new ApiError(409, {
            code: 'data_root_migration_blocked',
            detail: '/private/root must never be rendered',
            blockers: [
              {
                kind: 'unsafe_filesystem_entry',
                detail: '/private/raw detail',
                metadata: { relative_path: 'media/link' },
              },
            ],
          })
        }
      />,
    )

    expect(
      screen.getByText('The current state does not allow Data Root Migration.'),
    ).toBeVisible()
    expect(
      screen.getByText('The source contains an unsafe entry: media/link.'),
    ).toBeVisible()
    expect(screen.queryByText(/private/)).not.toBeInTheDocument()
  })
})
