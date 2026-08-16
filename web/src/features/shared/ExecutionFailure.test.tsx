import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { ExecutionFailure } from '@/features/shared/ExecutionFailure'
import i18n from '@/i18n'

afterEach(async () => {
  await i18n.changeLanguage('en-US')
})

describe('ExecutionFailure', () => {
  it('announces a stable English operation/status message', async () => {
    await i18n.changeLanguage('en-US')
    render(
      <ExecutionFailure
        operationType="rendering"
        ownerType="render_variant"
        status="failed"
      />,
    )

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Rendering did not complete. Check the source materials and render settings, then retry.',
    )
  })

  it('announces the matching Chinese owner/status fallback', async () => {
    await i18n.changeLanguage('zh-CN')
    render(<ExecutionFailure ownerType="run" status="interrupted" />)

    expect(screen.getByRole('alert')).toHaveTextContent(
      'ASTER 规划已中断，可以从上一个有效检查点继续。',
    )
  })
})
