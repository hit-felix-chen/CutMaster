/// <reference types="node" />

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { TimelineMemoryView } from '@/features/materials/memory/MemoryViews'
import '@/i18n'

const workspaceCss = readFileSync(
  resolve(process.cwd(), 'src/styles/workspace.css'),
  'utf8',
)

describe('TimelineMemoryView layout', () => {
  it('renders long Segment details outside the source video frame', () => {
    const stylesheet = document.createElement('style')
    stylesheet.textContent = workspaceCss
    document.head.append(stylesheet)
    const { container } = render(
      <TimelineMemoryView
        materialId="mat_video"
        payload={{
          source: { duration_sec: 120, title: 'Source' },
          segments: {
            items: [
              {
                segment_id: 'segment_0010',
                time_range: { start_sec: 10, end_sec: 20 },
                segment_summary: 'A long Segment description. '.repeat(80),
                emotional_tone: 'Reflective',
                narrative_function: 'Establishes the character arc',
                shots: Array.from({ length: 24 }, (_, index) => ({
                  shot_id: `shot_${String(index + 1).padStart(5, '0')}`,
                  time_range: { start_sec: 10 + index / 4, end_sec: 10.2 + index / 4 },
                })),
              },
            ],
          },
        }}
      />,
    )

    const video = container.querySelector('video')
    const mediaFrame = video?.parentElement
    const details = container.querySelector<HTMLElement>('.selection-inspector')
    const player = container.querySelector<HTMLElement>('.timeline-explorer__player')

    expect(video).not.toBeNull()
    expect(mediaFrame).toHaveClass('timeline-explorer__media')
    expect(mediaFrame?.parentElement).toBe(player)
    expect(details?.parentElement).toBe(player)
    expect(mediaFrame).not.toContainElement(details)
    expect(getComputedStyle(player!).gridTemplateRows).toBe(
      'minmax(260px, 68%) minmax(0, 1fr)',
    )
    expect(getComputedStyle(player!).placeContent).not.toBe('center')
    expect(getComputedStyle(details!).overflowY).toBe('auto')
    expect(getComputedStyle(video!).objectFit).toBe('contain')
    stylesheet.remove()
  })
})
