/// <reference types="node" />

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { TimelineMemoryView } from '@/features/materials/memory/MemoryViews'
import '@/i18n'

const workspaceCss = readFileSync(
  resolve(process.cwd(), 'src/styles/workspace.css'),
  'utf8',
)

describe('TimelineMemoryView layout', () => {
  it('keeps Segments left and nests the Shot column before the player in one right detail component', () => {
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
    const explorer = container.querySelector<HTMLElement>('.timeline-explorer')
    const player = container.querySelector<HTMLElement>('.timeline-explorer__player')
    const chooser = container.querySelector<HTMLElement>('.shot-strip')
    const detail = container.querySelector<HTMLElement>('.timeline-explorer__detail')
    const segments = container.querySelector<HTMLElement>(
      '.timeline-explorer__inspector',
    )
    const mainChildren = Array.from(explorer!.children).filter(
      (element) => !element.classList.contains('timeline-explorer__track'),
    )

    expect(video).not.toBeNull()
    expect(mediaFrame).toHaveClass('timeline-explorer__media')
    expect(mediaFrame?.parentElement).toBe(player)
    expect(inspector?.parentElement).toBe(player)
    expect(mediaFrame).not.toContainElement(details)
    expect(mainChildren).toEqual([segments, detail])
    expect(chooser?.parentElement).toBe(detail)
    expect(player?.parentElement).toBe(detail)
    expect(segments?.parentElement).toBe(explorer)
    expect(Array.from(detail!.children)).toEqual([chooser, player])
    expect(getComputedStyle(detail!).overflow).toBe('hidden')
    expect(getComputedStyle(player!).gridTemplateRows).toBe(
      'minmax(260px, 65%) minmax(160px, 1fr)',
    )
    expect(getComputedStyle(player!).placeContent).not.toBe('center')
    expect(getComputedStyle(details!).overflowY).toBe('auto')
    expect(getComputedStyle(video!).objectFit).toBe('contain')
    stylesheet.remove()
  })

  it('renders the Shot chooser as its own vertical scrolling column', () => {
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
    const detail = container.querySelector<HTMLElement>('.timeline-explorer__detail')
    const details = container.querySelector<HTMLElement>(
      '.selection-inspector__details',
    )

    expect(inspector).not.toBeNull()
    expect(chooser?.parentElement).toBe(detail)
    expect(details?.parentElement).toBe(inspector)
    expect(chooser).not.toContainElement(details)
    expect(getComputedStyle(inspector!).overflowY).toBe('hidden')
    expect(getComputedStyle(details!).overflowY).toBe('auto')
    expect(getComputedStyle(chooser!).flexDirection).toBe('column')
    expect(getComputedStyle(chooser!).overflowY).toBe('auto')
    stylesheet.remove()
  })

  it('scales through the one-hour boundary and scrolls longer source timelines', () => {
    const stylesheet = document.createElement('style')
    stylesheet.textContent = workspaceCss
    document.head.append(stylesheet)
    const { container, rerender } = render(
      <TimelineMemoryView
        materialId="mat_video"
        payload={{
          source: { duration_sec: 3599, title: 'Do not show this title' },
          segments: {
            items: [
              {
                segment_id: 'segment_0001',
                time_range: { start_sec: 0, end_sec: 300 },
                shots: [],
              },
            ],
          },
        }}
      />,
    )

    const track = container.querySelector<HTMLElement>('.timeline-explorer__track')
    expect(track).toHaveAttribute('role', 'region')
    expect(container.querySelector<HTMLElement>('.source-track__canvas')).toHaveStyle({
      width: '100%',
    })
    expect(getComputedStyle(track!).overflowX).toBe('auto')
    expect(screen.getByText('00:00')).toBeVisible()
    expect(screen.getByText('10:00')).toBeVisible()
    expect(screen.getByText('59:59')).toBeVisible()
    expect(screen.queryByText('Do not show this title')).not.toBeInTheDocument()

    rerender(
      <TimelineMemoryView
        materialId="mat_video"
        payload={{
          source: { duration_sec: 601 },
          segments: { items: [] },
        }}
      />,
    )
    expect(screen.queryByText('10:00')).not.toBeInTheDocument()
    expect(screen.getByText('10:01')).toBeVisible()

    rerender(
      <TimelineMemoryView
        materialId="mat_video"
        payload={{
          source: { duration_sec: 3600 },
          segments: { items: [] },
        }}
      />,
    )
    expect(container.querySelector<HTMLElement>('.source-track__canvas')).toHaveStyle({
      width: '100%',
    })
    expect(screen.getByText('1:00:00')).toBeVisible()

    rerender(
      <TimelineMemoryView
        materialId="mat_video"
        payload={{
          source: { duration_sec: 3601 },
          segments: { items: [] },
        }}
      />,
    )
    expect(
      Number.parseFloat(
        container.querySelector<HTMLElement>('.source-track__canvas')!.style.width,
      ),
    ).toBeGreaterThan(100)
    expect(container.querySelector<HTMLElement>('.source-track__canvas')).toHaveClass(
      'source-track__canvas--scrollable',
    )
    expect(screen.queryByText('1:00:00')).not.toBeInTheDocument()
    expect(screen.getByText('1:00:01')).toBeVisible()

    rerender(
      <TimelineMemoryView
        materialId="mat_video"
        payload={{
          source: { duration_sec: 3720 },
          segments: { items: [] },
        }}
      />,
    )
    expect(screen.queryByText('1:00:00')).not.toBeInTheDocument()
    expect(screen.getByText('1:02:00')).toBeVisible()

    rerender(
      <TimelineMemoryView
        materialId="mat_video"
        payload={{
          source: { duration_sec: 3900 },
          segments: { items: [] },
        }}
      />,
    )
    expect(screen.getByText('1:00:00')).toBeVisible()
    expect(screen.getByText('1:05:00')).toBeVisible()

    rerender(
      <TimelineMemoryView
        materialId="mat_video"
        payload={{
          source: { duration_sec: 7201, title: 'Still hidden' },
          segments: {
            items: [
              {
                segment_id: 'segment_0001',
                time_range: { start_sec: 0, end_sec: 300 },
                shots: [],
              },
            ],
          },
        }}
      />,
    )

    expect(
      Number.parseFloat(
        container.querySelector<HTMLElement>('.source-track__canvas')!.style.width,
      ),
    ).toBeGreaterThan(200)
    expect(screen.queryByText('2:00:00')).not.toBeInTheDocument()
    expect(screen.getByText('2:00:01')).toBeVisible()
    expect(screen.queryByText('Still hidden')).not.toBeInTheDocument()
    stylesheet.remove()
  })

  it('clamps a Segment box to the next Segment start so boxes never overlap', () => {
    const stylesheet = document.createElement('style')
    stylesheet.textContent = workspaceCss
    document.head.append(stylesheet)
    const { container } = render(
      <TimelineMemoryView
        materialId="mat_video"
        payload={{
          source: { duration_sec: 1000 },
          segments: {
            items: [
              {
                segment_id: 'segment_0002',
                time_range: { start_sec: 450, end_sec: 1800 },
                shots: [],
              },
              {
                segment_id: 'segment_0001',
                time_range: { start_sec: -100, end_sec: 500 },
                shots: [],
              },
              {
                segment_id: 'segment_0003',
                time_range: { start_sec: 1200, end_sec: 1400 },
                shots: [],
              },
              {
                segment_id: 'segment_without_range',
                shots: [],
              },
            ],
          },
        }}
      />,
    )
    const boxes = Array.from(
      container.querySelectorAll<HTMLElement>('.source-track__segment'),
    )
    const firstRight =
      Number.parseFloat(boxes[0].style.left) + Number.parseFloat(boxes[0].style.width)
    const secondLeft = Number.parseFloat(boxes[1].style.left)
    const secondRight =
      Number.parseFloat(boxes[1].style.left) + Number.parseFloat(boxes[1].style.width)
    const thirdLeft = Number.parseFloat(boxes[2].style.left)
    const listedIds = Array.from(
      container.querySelectorAll<HTMLElement>('.segment-item strong'),
      (element) => element.textContent,
    )

    expect(firstRight).toBeLessThanOrEqual(secondLeft)
    expect(secondRight).toBeLessThanOrEqual(thirdLeft)
    expect(boxes[0]).toHaveStyle({ left: '0%' })
    expect(boxes[2]).toHaveStyle({ left: '100%', width: '0%' })
    expect(boxes[0].style.minWidth).toBe('')
    expect(boxes[0]).toHaveAttribute('tabindex', '-1')
    expect(getComputedStyle(boxes[0]).padding).toBe('0px')
    expect(getComputedStyle(boxes[0]).borderWidth).toBe('0px')
    expect(listedIds).toEqual([
      'segment_0001',
      'segment_0002',
      'segment_0003',
      'segment_without_range',
    ])
    stylesheet.remove()
  })

  it('normalizes an invalid duration and renders an empty source safely', () => {
    const { container } = render(
      <TimelineMemoryView
        materialId="mat_video"
        payload={{
          source: { duration_sec: 0 },
          segments: { items: [] },
        }}
      />,
    )

    expect(container.querySelectorAll('.source-track__segment')).toHaveLength(0)
    expect(container.querySelector('.timeline-explorer')).toHaveClass(
      'timeline-explorer--no-shots',
    )
    expect(container.querySelector('.shot-strip')).not.toBeInTheDocument()
    const detail = container.querySelector('.timeline-explorer__detail')
    expect(detail).toHaveClass('timeline-explorer__detail--no-shots')
    expect(Array.from(detail!.children)).toEqual([
      container.querySelector('.timeline-explorer__player'),
    ])
    expect(container.querySelector<HTMLElement>('.source-track__canvas')).toHaveStyle({
      width: '100%',
    })
    expect(screen.getByText('00:00')).toBeVisible()
    expect(screen.getByText('0:01')).toBeVisible()
  })

  it('seeks to a Shot start and can return to its Segment start', () => {
    const onSelectionChange = vi.fn()
    const { container } = render(
      <TimelineMemoryView
        materialId="mat_video"
        onSelectionChange={onSelectionChange}
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
    const shotButton = screen.getByRole('button', { name: /shot_00002/ })
    expect(shotButton).toHaveAttribute('aria-pressed', 'false')
    fireEvent.click(shotButton)
    expect(video!.currentTime).toBe(15.5)
    expect(shotButton).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('heading', { name: 'shot_00002' })).toBeVisible()
    expect(onSelectionChange).toHaveBeenLastCalledWith('segment_0010', 'shot_00002')
    video!.currentTime = 0
    fireEvent.loadedMetadata(video!)
    expect(video!.currentTime).toBe(15.5)

    fireEvent.click(screen.getByRole('button', { name: 'Back to Segment' }))
    expect(video!.currentTime).toBe(10)
    expect(screen.getByRole('heading', { name: 'segment_0010' })).toBeVisible()
    expect(onSelectionChange).toHaveBeenLastCalledWith('segment_0010')
    video!.currentTime = 0
    fireEvent.loadedMetadata(video!)
    expect(video!.currentTime).toBe(10)
  })
})
