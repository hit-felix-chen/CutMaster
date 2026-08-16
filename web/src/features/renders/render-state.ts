import type {
  ExecutionSummary,
  RenderAudioMode,
  RenderVariant,
} from '@/features/shared/api'

const activeVariantStatuses = new Set(['queued', 'rendering'])
const activeAttemptStatuses = new Set(['queued', 'running', 'retrying', 'stopping'])

export type RenderRecoveryAction = 'retry' | 'resume' | 'renderAgain'

export function isRenderVariantActive(variant: RenderVariant): boolean {
  return activeVariantStatuses.has(variant.status)
}

export function isRenderExecutionActive(
  variant: RenderVariant,
  execution: ExecutionSummary | null,
): boolean {
  if (isRenderVariantActive(variant)) return true
  return Boolean(
    execution && activeAttemptStatuses.has(execution.attempt.status.toLowerCase()),
  )
}

export function renderRecoveryAction(
  variant: RenderVariant,
): RenderRecoveryAction | null {
  switch (variant.status) {
    case 'failed':
      return 'retry'
    case 'interrupted':
      return 'resume'
    case 'unavailable':
      return 'renderAgain'
    default:
      return null
  }
}

export function renderAudioMode(variant: RenderVariant): RenderAudioMode {
  return variant.specification.audio_mode
}

export function findAudioModeVariant(
  variants: RenderVariant[],
  audioMode: RenderAudioMode,
): RenderVariant | undefined {
  return variants.find((variant) => renderAudioMode(variant) === audioMode)
}
