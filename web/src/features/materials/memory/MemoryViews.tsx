import { AudioWaveform, Captions, Clock3, Film, Gauge, Music2 } from 'lucide-react'
import { useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import { formatDuration, formatFrameRate, formatNumber } from '@/i18n/formatters'

type DataRecord = Record<string, unknown>

function record(value: unknown): DataRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as DataRecord)
    : {}
}

function recordsPage(value: unknown): DataRecord[] {
  const items = record(value).items
  return Array.isArray(items) ? items.map(record) : []
}

function numberValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function textArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : []
}

function timeRange(value: unknown) {
  const range = record(value)
  return {
    start: numberValue(range.start_sec) ?? 0,
    end: numberValue(range.end_sec) ?? 0,
  }
}

function clock(value: number) {
  const seconds = Math.max(0, Math.round(value))
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const remainder = seconds % 60
  return hours
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
    : `${minutes}:${String(remainder).padStart(2, '0')}`
}

export function TimelineMemoryView({
  materialId,
  payload,
}: {
  materialId: string
  payload: DataRecord
}) {
  const { t } = useTranslation('common')
  const segments = recordsPage(payload.segments)
  const [selectedId, setSelectedId] = useState(() => text(segments[0]?.segment_id))
  const [selectedShotId, setSelectedShotId] = useState('')
  const player = useRef<HTMLVideoElement>(null)
  const source = record(payload.source)
  const sourceDuration =
    numberValue(source.duration_sec) ??
    Math.max(1, ...segments.map((item) => timeRange(item.time_range).end))
  const selected =
    segments.find((item) => text(item.segment_id) === selectedId) ?? segments[0]
  const shots = Array.isArray(selected?.shots) ? selected.shots.map(record) : []
  const selectedShot = shots.find((shot) => text(shot.shot_id) === selectedShotId)

  const selectSegment = (segment: DataRecord) => {
    setSelectedId(text(segment.segment_id))
    setSelectedShotId('')
    const start = timeRange(segment.time_range).start
    if (player.current) player.current.currentTime = start
  }
  const selectShot = (shot: DataRecord) => {
    setSelectedShotId(text(shot.shot_id))
    const start = timeRange(shot.time_range).start
    if (player.current) player.current.currentTime = start
  }

  return (
    <div className="timeline-explorer">
      <aside className="timeline-explorer__inspector">
        <header className="memory-pane-heading">
          <Film size={16} />
          <h3>{t('materials.segments')}</h3>
          <span>{segments.length}</span>
        </header>
        <div className="segment-list">
          {segments.map((segment) => {
            const range = timeRange(segment.time_range)
            return (
              <button
                key={text(segment.segment_id)}
                type="button"
                className={
                  text(segment.segment_id) === text(selected?.segment_id)
                    ? 'segment-item segment-item--active'
                    : 'segment-item'
                }
                onClick={() => selectSegment(segment)}
              >
                <strong>{text(segment.segment_id)}</strong>
                <span>
                  {clock(range.start)} — {clock(range.end)}
                </span>
                <small>
                  {text(segment.segment_summary) || text(segment.narrative_function)}
                </small>
              </button>
            )
          })}
        </div>
      </aside>
      <section className="timeline-explorer__player">
        <div className="timeline-explorer__media">
          <video
            ref={player}
            controls
            muted
            preload="metadata"
            src={`/api/materials/${encodeURIComponent(materialId)}/source`}
          />
        </div>
        {selected ? (
          <div className="selection-inspector">
            <div>
              <span className="eyebrow">
                {selectedShot ? t('materials.shots') : t('materials.segments')}
              </span>
              <h3>{text(selectedShot?.shot_id) || text(selected.segment_id)}</h3>
            </div>
            <p>
              {text(selectedShot?.visual_description) ||
                text(selected.segment_summary) ||
                text(selected.narrative_function)}
            </p>
            <div className="selection-metadata">
              <span>
                {text(selectedShot?.emotional_tone) || text(selected.emotional_tone)}
              </span>
              <span>
                {text(selectedShot?.dominant_action) ||
                  text(selected.narrative_function)}
              </span>
            </div>
            {shots.length ? (
              <div className="shot-strip">
                {shots.map((shot) => (
                  <button
                    type="button"
                    key={text(shot.shot_id)}
                    className={
                      text(shot.shot_id) === selectedShotId
                        ? 'shot-chip shot-chip--active'
                        : 'shot-chip'
                    }
                    onClick={() => selectShot(shot)}
                  >
                    <strong>{text(shot.shot_id)}</strong>
                    <small>{clock(timeRange(shot.time_range).start)}</small>
                  </button>
                ))}
              </div>
            ) : null}
          </div>
        ) : null}
      </section>
      <div className="timeline-explorer__track" aria-label={t('materials.timeline')}>
        <div className="source-track">
          {segments.map((segment) => {
            const range = timeRange(segment.time_range)
            return (
              <button
                key={text(segment.segment_id)}
                type="button"
                className={
                  text(segment.segment_id) === text(selected?.segment_id)
                    ? 'source-track__segment source-track__segment--active'
                    : 'source-track__segment'
                }
                style={{
                  left: `${(range.start / sourceDuration) * 100}%`,
                  width: `${Math.max(0.2, ((range.end - range.start) / sourceDuration) * 100)}%`,
                }}
                onClick={() => selectSegment(segment)}
                aria-label={text(segment.segment_id)}
              />
            )
          })}
        </div>
        <div className="source-track__legend">
          <span>00:00</span>
          <span>{text(source.title)}</span>
          <span>{clock(sourceDuration)}</span>
        </div>
      </div>
    </div>
  )
}

export function StoryMemoryView({ payload }: { payload: DataRecord }) {
  const { t } = useTranslation('common')
  const beats = Array.isArray(payload.chronological_story_beats)
    ? payload.chronological_story_beats.map(record)
    : []
  const arcs = Array.isArray(payload.character_arcs)
    ? payload.character_arcs.map(record)
    : []
  return (
    <div className="story-memory">
      <header className="story-hero">
        <span className="eyebrow">{t('materials.story')}</span>
        <h2>{text(payload.title) || t('common.unknown')}</h2>
        <p className="story-logline">{text(payload.logline)}</p>
      </header>
      <section className="story-synopsis">
        <h3>{t('materials.synopsis')}</h3>
        <p>{text(payload.synopsis)}</p>
      </section>
      <section>
        <h3 className="memory-section-title">{t('materials.storyBeats')}</h3>
        <div className="story-beats">
          {beats.map((beat, index) => (
            <article key={index}>
              <span className="story-beat__index">
                {String(index + 1).padStart(2, '0')}
              </span>
              <p>{text(beat.summary)}</p>
              <small>{text(beat.narrative_significance)}</small>
              <div>
                {textArray(beat.characters).map((name) => (
                  <span key={name}>{name}</span>
                ))}
              </div>
            </article>
          ))}
        </div>
      </section>
      <div className="story-columns">
        <section>
          <h3>{t('materials.characterArcs')}</h3>
          {arcs.map((arc, index) => (
            <article className="arc-card" key={index}>
              <strong>{text(arc.character) || text(arc.name)}</strong>
              <p>{text(arc.arc) || text(arc.summary)}</p>
            </article>
          ))}
        </section>
        <section>
          <h3>{t('materials.themes')}</h3>
          <div className="theme-list">
            {textArray(payload.themes).map((theme) => (
              <span key={theme}>{theme}</span>
            ))}
          </div>
          <h3>{t('materials.ending')}</h3>
          <p>{text(payload.ending)}</p>
        </section>
      </div>
    </div>
  )
}

export function DialogueMemoryView({ payload }: { payload: DataRecord }) {
  const { t } = useTranslation('common')
  const [query, setQuery] = useState('')
  const [speaker, setSpeaker] = useState('')
  const sentences = recordsPage(payload.sentences)
  const speakers = useMemo(
    () =>
      Array.from(
        new Set(sentences.map((item) => text(item.speaker)).filter(Boolean)),
      ).sort(),
    [sentences],
  )
  const filtered = sentences.filter(
    (item) =>
      (!speaker || text(item.speaker) === speaker) &&
      (!query ||
        text(item.text).toLocaleLowerCase().includes(query.toLocaleLowerCase())),
  )
  return (
    <div className="dialogue-memory">
      <div className="dialogue-toolbar">
        <label>
          <span className="sr-only">{t('common.search')}</span>
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t('common.search')}
          />
        </label>
        <label>
          <span className="sr-only">{t('materials.speaker')}</span>
          <select value={speaker} onChange={(event) => setSpeaker(event.target.value)}>
            <option value="">{t('materials.speaker')}</option>
            {speakers.map((name) => (
              <option key={name}>{name}</option>
            ))}
          </select>
        </label>
        <span>
          <Captions size={15} />
          {filtered.length}
        </span>
      </div>
      <div className="dialogue-list">
        {filtered.map((sentence, index) => (
          <article key={text(sentence.sentence_id) || index}>
            <time>
              {text(sentence.start)} — {text(sentence.end)}
            </time>
            <strong>{text(sentence.speaker) || t('common.unknown')}</strong>
            <p>{text(sentence.text)}</p>
          </article>
        ))}
      </div>
    </div>
  )
}

export function TechnicalMemoryView({ payload }: { payload: DataRecord }) {
  const { t, i18n } = useTranslation('common')
  const source = record(payload.source)
  const models = record(payload.models)
  const duration = numberValue(source.duration_sec ?? payload.source_duration_sec)
  const fps = numberValue(source.fps)
  const tempo = numberValue(payload.tempo_bpm)
  const metrics: Array<[string, string | number | null]> = [
    [
      t('common.duration'),
      duration === null ? null : formatDuration(duration, i18n.language),
    ],
    [
      t('materials.dimensions'),
      source.width && source.height ? `${source.width} × ${source.height}` : null,
    ],
    [t('materials.fps'), fps === null ? null : formatFrameRate(fps, i18n.language)],
    [
      t('materials.tempo'),
      tempo === null ? null : `${formatNumber(tempo, i18n.language)} BPM`,
    ],
    [t('materials.segments'), numberValue(payload.segment_count)],
    [t('materials.shots'), numberValue(payload.shot_count)],
    [t('materials.dialogue'), numberValue(payload.dialogue_count)],
    [
      t('materials.providerRejectedCount'),
      numberValue(payload.provider_rejected_shot_count),
    ],
    [t('materials.beats'), numberValue(payload.beat_count)],
    [t('materials.accents'), numberValue(payload.accent_count)],
    [t('materials.sections'), numberValue(payload.section_count)],
    [t('materials.schema'), text(payload.schema_version) || null],
  ]
  return (
    <div className="technical-memory">
      <div className="metric-grid">
        {metrics
          .filter(([, value]) => value !== null && value !== undefined)
          .map(([label, value]) => (
            <article key={label}>
              <Gauge size={17} />
              <span>{label}</span>
              <strong>
                {typeof value === 'number' ? formatNumber(value, i18n.language) : value}
              </strong>
            </article>
          ))}
      </div>
      {Object.keys(models).length ? (
        <section className="model-list">
          <h3>{t('materials.models')}</h3>
          {Object.entries(models).map(([name, value]) => (
            <div key={name}>
              <span>{name}</span>
              <code>{String(value ?? '—')}</code>
            </div>
          ))}
        </section>
      ) : null}
    </div>
  )
}

export function MusicStructureView({
  materialId,
  payload,
}: {
  materialId: string
  payload: DataRecord
}) {
  const { t } = useTranslation('common')
  const duration = numberValue(payload.source_duration_sec) ?? 1
  const sections = recordsPage(payload.sections)
  const energy = recordsPage(payload.energy_curve)
  const beats = record(payload.beats_sec).items
  const accents = record(payload.accents_sec).items
  return (
    <div className="music-structure">
      {/* Music Material Memory contains no speech track, so a caption track is not applicable. */}
      {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
      <audio
        className="structure-audio"
        controls
        preload="metadata"
        src={`/api/materials/${encodeURIComponent(materialId)}/source`}
      />
      <div className="music-metrics">
        <article>
          <Music2 size={17} />
          <span>{t('materials.tempo')}</span>
          <strong>{numberValue(payload.tempo_bpm)?.toFixed(1) ?? '—'} BPM</strong>
        </article>
        <article>
          <Clock3 size={17} />
          <span>{t('common.duration')}</span>
          <strong>{clock(duration)}</strong>
        </article>
        <article>
          <AudioWaveform size={17} />
          <span>{t('materials.beats')}</span>
          <strong>{Array.isArray(beats) ? beats.length : 0}</strong>
        </article>
      </div>
      <section className="energy-panel">
        <header>
          <h3>{t('materials.energy')}</h3>
          <span>
            {t('materials.accents')}: {Array.isArray(accents) ? accents.length : 0}
          </span>
        </header>
        <div className="energy-bars">
          {energy.map((point, index) => (
            <i
              key={index}
              style={{
                height: `${Math.max(3, (numberValue(point.energy) ?? 0) * 100)}%`,
              }}
              title={`${numberValue(point.time_sec) ?? 0}s`}
            />
          ))}
        </div>
      </section>
      <section>
        <h3 className="memory-section-title">{t('materials.sections')}</h3>
        <div className="music-sections">
          {sections.map((section, index) => {
            const start = numberValue(section.start_sec) ?? 0
            const end = numberValue(section.end_sec) ?? 0
            return (
              <article key={text(section.section_id) || index}>
                <div
                  className="music-section__tone"
                  style={{ width: `${Math.max(3, ((end - start) / duration) * 100)}%` }}
                />
                <header>
                  <strong>{text(section.role) || text(section.section_id)}</strong>
                  <time>
                    {clock(start)} — {clock(end)}
                  </time>
                </header>
                <p>
                  {text(section.energy_trend)} ·{' '}
                  {numberValue(section.mean_energy)?.toFixed(2)}
                </p>
              </article>
            )
          })}
        </div>
      </section>
    </div>
  )
}

export function MemoryTabView({
  type,
  tab,
  materialId,
  payload,
}: {
  type: 'video' | 'music'
  tab: string
  materialId: string
  payload: DataRecord
}) {
  if (type === 'video' && tab === 'timeline')
    return <TimelineMemoryView materialId={materialId} payload={payload} />
  if (type === 'video' && tab === 'story') return <StoryMemoryView payload={payload} />
  if (type === 'video' && tab === 'dialogue')
    return <DialogueMemoryView payload={payload} />
  if (type === 'music' && tab === 'structure')
    return <MusicStructureView materialId={materialId} payload={payload} />
  return <TechnicalMemoryView payload={payload} />
}
