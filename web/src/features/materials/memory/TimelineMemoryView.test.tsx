/// <reference types="node" />

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { fireEvent, render, screen } from '@testing-library/react'
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
    const inspector = container.querySelector<HTMLElement>('.selection-inspector')
    const details = container.querySelector<HTMLElement>(
      '.selection-inspector__details',
    )
    const player = container.querySelector<HTMLElement>('.timeline-explorer__player')

    expect(video).not.toBeNull()
    expect(mediaFrame).toHaveClass('timeline-explorer__media')
    expect(mediaFrame?.parentElement).toBe(player)
    expect(inspector?.parentElement).toBe(player)
    expect(mediaFrame).not.toContainElement(details)
    expect(getComputedStyle(player!).gridTemplateRows).toBe(
      'minmax(260px, 65%) minmax(160px, 1fr)',
    )
    expect(getComputedStyle(player!).placeContent).not.toBe('center')
    expect(getComputedStyle(details!).overflowY).toBe('auto')
    expect(getComputedStyle(video!).objectFit).toBe('contain')
    stylesheet.remove()
  })

  it('keeps the Shot chooser outside the scrollable description', () => {
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
                shots: Array.from({ length: 24 }, (_, index) => ({
                  shot_id: `shot_${String(index + 1).padStart(5, '0')}`,
                  time_range: {
                    start_sec: 10 + index / 4,
                    end_sec: 10.2 + index / 4,
                  },
                })),
              },
            ],
          },
        }}
      />,
    )

    const inspector = container.querySelector<HTMLElement>('.selection-inspector')
    const chooser = container.querySelector<HTMLElement>('.shot-strip')
    const details = container.querySelector<HTMLElement>(
      '.selection-inspector__details',
    )

    expect(inspector).not.toBeNull()
    expect(chooser?.parentElement).toBe(inspector)
    expect(details?.parentElement).toBe(inspector)
    expect(chooser).not.toContainElement(details)
    expect(getComputedStyle(inspector!).overflowY).toBe('hidden')
    expect(getComputedStyle(details!).overflowY).toBe('auto')
    expect(getComputedStyle(chooser!).overflowY).toBe('hidden')
    stylesheet.remove()
  })

  it('seeks to a Shot start and can return to its Segment start', () => {
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
                segment_summary: 'Segment summary',
                shots: [
                  {
                    shot_id: 'shot_00001',
                    time_range: { start_sec: 10, end_sec: 14 },
                  },
                  {
                    shot_id: 'shot_00002',
                    time_range: { start_sec: 15.5, end_sec: 18 },
                  },
                ],
              },
            ],
          },
        }}
      />,
    )
    const video = container.querySelector('video')

    expect(video).not.toBeNull()
    fireEvent.click(screen.getByRole('button', { name: /shot_00002/ }))
    expect(video!.currentTime).toBe(15.5)
    expect(screen.getByRole('heading', { name: 'shot_00002' })).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: 'Back to Segment' }))
    expect(video!.currentTime).toBe(10)
    expect(screen.getByRole('heading', { name: 'segment_0010' })).toBeVisible()
  })
})
