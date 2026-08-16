import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import {
  DialogueMemoryView,
  MusicStructureView,
  StoryMemoryView,
  TimelineMemoryView,
} from '@/features/materials/memory/MemoryViews'
import '@/i18n'

describe('Material Memory projections', () => {
  it('shows the real Segment and Shot annotations returned by Timeline Memory', () => {
    render(
      <TimelineMemoryView
        materialId="mat_video"
        payload={{
          source: { duration_sec: 60, title: 'Source' },
          segments: {
            items: [
              {
                segment_id: 'segment_0001',
                time_range: { start_sec: 5, end_sec: 20 },
                segment_summary: 'The arrival changes the room.',
                appearing_characters: ['Mia'],
                dialogue_items: [
                  {
                    dialogue_id: 'dialogue_1',
                    speaker: 'Mia',
                    text: 'I made it.',
                    time_range: { start_sec: 6, end_sec: 7 },
                  },
                ],
                shots: [
                  {
                    shot_id: 'shot_00001',
                    time_range: { start_sec: 8.25, end_sec: 12 },
                    visual_description: 'Mia enters through the blue doorway.',
                    dominant_action: 'enters',
                    shot_scale: 'medium',
                    camera_angle: 'eye_level',
                    camera_movement: 'tracking',
                    composition: 'Mia is framed on the left third.',
                    scene: {
                      interior_exterior: 'interior',
                      location: 'jazz club',
                      time_of_day: 'night',
                      color_palette: ['blue', 'amber'],
                    },
                    characters: [
                      {
                        character_id: 'mia',
                        name: 'Mia',
                        description: 'A pianist in a dark coat.',
                        identity_evidence: 'Her face is clearly visible.',
                      },
                    ],
                    dialogue: [],
                    visual_evidence: 'Five samples show the same entrance.',
                    sampled_frame_times_sec: [8.25, 9, 10, 11, 11.75],
                  },
                ],
              },
            ],
          },
        }}
      />,
    )

    expect(screen.getAllByText('Mia')).not.toHaveLength(0)
    expect(screen.getByText('I made it.')).toBeVisible()

    fireEvent.click(screen.getByRole('button', { name: /shot_00001/ }))
    expect(screen.getByText('Mia enters through the blue doorway.')).toBeVisible()
    expect(screen.getByText('tracking')).toBeVisible()
    expect(screen.getByText('jazz club')).toBeVisible()
    expect(screen.getByText('Mia is framed on the left third.')).toBeVisible()
    expect(screen.getByText('Five samples show the same entrance.')).toBeVisible()
    expect(screen.getAllByText('0:08')).not.toHaveLength(0)
  })

  it('opens Story evidence using its exact source Segment id', () => {
    const open = vi.fn()
    render(
      <StoryMemoryView
        payload={{
          title: 'Story',
          chronological_story_beats: [
            {
              summary: 'The turning point.',
              source_segment_ids: ['segment_0042'],
            },
          ],
          character_arcs: [],
          themes: [],
        }}
        onOpenTimelineSegment={open}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'segment_0042' }))
    expect(open).toHaveBeenCalledWith('segment_0042')
  })

  it('filters Dialogue and seeks while playback follows the active sentence', () => {
    const { container } = render(
      <DialogueMemoryView
        materialId="mat_video"
        payload={{
          sentences: {
            items: [
              {
                sentence_id: 1,
                speaker: 'Mia',
                start: '00:00:01,250',
                end: '00:00:03,000',
                text: 'I am ready.',
              },
              {
                sentence_id: 2,
                speaker: 'Sebastian',
                start: '00:00:05,500',
                end: '00:00:08,000',
                text: 'Then let us begin.',
              },
            ],
            total: 2,
          },
        }}
      />,
    )
    const video = container.querySelector('video')

    fireEvent.click(screen.getByRole('button', { name: /Then let us begin/ }))
    expect(video?.currentTime).toBe(5.5)

    if (video) {
      video.currentTime = 1.5
      fireEvent.timeUpdate(video)
    }
    expect(screen.getByRole('button', { name: /I am ready/ })).toHaveAttribute(
      'aria-pressed',
      'true',
    )

    fireEvent.change(screen.getByRole('combobox'), {
      target: { value: 'Sebastian' },
    })
    expect(screen.queryByText('I am ready.')).not.toBeInTheDocument()
    expect(screen.getByText('Then let us begin.')).toBeVisible()
  })

  it('seeks real Section, beat and accent markers and shows clip ranges', () => {
    const { container } = render(
      <MusicStructureView
        materialId="mat_music"
        payload={{
          source_duration_sec: 60,
          tempo_bpm: 120,
          beats_sec: { items: [12], total: 1 },
          accents_sec: { items: [18], total: 1 },
          energy_curve: {
            items: [{ time_sec: 0, energy: 0.5 }],
            total: 1,
          },
          sections: {
            items: [
              {
                section_id: 'music_01',
                start_sec: 10,
                end_sec: 30,
                role: 'build',
                mean_energy: 0.6,
                energy_trend: 'rising',
                suggested_clip_duration_sec: [3.5, 4.9],
              },
            ],
            total: 1,
          },
        }}
      />,
    )
    const audio = container.querySelector('audio')

    fireEvent.click(screen.getByRole('button', { name: /beat at 0:12/i }))
    expect(audio?.currentTime).toBe(12)
    fireEvent.click(screen.getByRole('button', { name: /accent at 0:18/i }))
    expect(audio?.currentTime).toBe(18)
    fireEvent.click(screen.getByRole('button', { name: /build at 0:10/i }))
    expect(audio?.currentTime).toBe(10)
    expect(screen.getByText(/Suggested clip duration: 3.5s . 4.9s/)).toBeVisible()
  })
})
