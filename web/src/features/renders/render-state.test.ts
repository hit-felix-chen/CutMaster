import { describe, expect, it } from 'vitest'

import {
  isRenderExecutionActive,
  renderRecoveryAction,
} from '@/features/renders/render-state'
import type { ExecutionSummary, RenderVariant } from '@/features/shared/api'

function variant(status: RenderVariant['status']): RenderVariant {
  return {
    render_variant_id: `variant_${status}`,
    project_id: 'project_1',
    run_id: 'run_1',
    run_sequence: 1,
    edit_id: 'edit_1',
    edit_sequence: 1,
    edit_origin: 'initial',
    status,
    specification: {
      schema_version: '1.0',
      audio_mode: 'dialogue',
      renderer: {
        width: 1920,
        height: 1080,
        fps: 30,
        encoder: 'libx264',
        threads: 8,
        bgm_volume: 0.3,
        original_volume: 0,
        audio_sample_rate: 48000,
        dialogue_audio: {},
      },
    },
    frame_count: null,
    duration_sec: null,
    size_bytes: null,
    failure_message: null,
    media_url: null,
    download_url: null,
    created_at: '2026-08-16T00:00:00Z',
    updated_at: '2026-08-16T00:00:00Z',
  }
}

function execution(status: string): ExecutionSummary {
  return {
    attempt: {
      attempt_id: 'attempt_1',
      operation_type: 'renderer',
      owner_type: 'render_variant',
      owner_id: 'variant_1',
      sequence: 1,
      status,
      created_at: '2026-08-16T00:00:00Z',
      updated_at: '2026-08-16T00:00:00Z',
    },
    job: {
      job_id: 'job_1',
      attempt_id: 'attempt_1',
      status,
      stop_requested: status === 'stopping',
      progress: {},
      created_at: '2026-08-16T00:00:00Z',
      updated_at: '2026-08-16T00:00:00Z',
    },
  }
}

describe('Render Variant lifecycle', () => {
  it.each([
    ['failed', 'retry'],
    ['interrupted', 'resume'],
    ['unavailable', 'renderAgain'],
    ['ready', null],
    ['queued', null],
    ['rendering', null],
  ] as const)('maps %s to only its valid recovery action', (status, action) => {
    expect(renderRecoveryAction(variant(status))).toBe(action)
  })

  it('keeps queued, rendering and stopping executions live for real polling', () => {
    expect(isRenderExecutionActive(variant('queued'), null)).toBe(true)
    expect(isRenderExecutionActive(variant('rendering'), execution('running'))).toBe(
      true,
    )
    expect(isRenderExecutionActive(variant('interrupted'), execution('stopping'))).toBe(
      true,
    )
    expect(isRenderExecutionActive(variant('ready'), execution('complete'))).toBe(false)
  })
})
