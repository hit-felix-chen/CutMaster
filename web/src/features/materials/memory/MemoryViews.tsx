import {
  AudioWaveform,
  Camera,
  Captions,
  ChevronLeft,
  Clock3,
  Eye,
  Film,
  Gauge,
  MapPin,
  MessageSquareText,
  Music2,
  Users,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
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

function records(value: unknown): DataRecord[] {
  return Array.isArray(value) ? value.map(record) : []
}

function numericItems(value: unknown): number[] {
  const items = record(value).items
  return Array.isArray(items)
    ? items.filter(
        (item): item is number => typeof item === 'number' && Number.isFinite(item),
      )
    : []
}

function pageTotal(value: unknown) {
  const page = record(value)
  return numberValue(page.total) ?? recordsPage(value).length
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

function parseTimecode(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value !== 'string') return null
  const parts = value.trim().replace('.', ',').split(':')
  if (parts.length !== 3) return null
  const hours = Number(parts[0])
  const minutes = Number(parts[1])
  const seconds = Number(parts[2].replace(',', '.'))
  if (![hours, minutes, seconds].every(Number.isFinite)) return null
  return hours * 3600 + minutes * 60 + seconds
}

function dialogueRange(value: DataRecord) {
  const range = timeRange(value.time_range)
  return {
    start: numberValue(value.start_sec) ?? parseTimecode(value.start) ?? range.start,
    end: numberValue(value.end_sec) ?? parseTimecode(value.end) ?? range.end,
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

function values(...items: unknown[]) {
  return items
    .flatMap((item) => (Array.isArray(item) ? item : [item]))
    .filter(
      (item): item is string | number =>
        (typeof item === 'string' && Boolean(item.trim())) ||
        (typeof item === 'number' && Number.isFinite(item)),
    )
}

function DetailChips({ items }: { items: Array<string | number> }) {
  if (!items.length) return null
  return (
    <div className="selection-metadata">
      {items.map((item, index) => (
        <span key={`${item}-${index}`}>{item}</span>
      ))}
    </div>
  )
}

function DialogueCards({ items }: { items: DataRecord[] }) {
  const { t } = useTranslation('common')
  if (!items.length) return null
  return (
    <section className="memory-detail-section">
      <h4>
        <MessageSquareText size={14} aria-hidden="true" />
        {t('materials.dialogue')}
      </h4>
      <div className="memory-dialogue-cards">
        {items.map((item, index) => {
          const range = timeRange(item.time_range)
          return (
            <article key={text(item.dialogue_id) || index}>
              <header>
                <strong>{text(item.speaker) || t('common.unknown')}</strong>
                {range.end > range.start ? (
                  <time>
                    {clock(range.start)} — {clock(range.end)}
                  </time>
                ) : null}
              </header>
              <p>{text(item.text)}</p>
            </article>
          )
        })}
      </div>
    </section>
  )
}

function CharacterCards({ items }: { items: DataRecord[] }) {
  const { t } = useTranslation('common')
  if (!items.length) return null
  return (
    <section className="memory-detail-section">
      <h4>
        <Users size={14} aria-hidden="true" />
        {t('materials.characters')}
      </h4>
      <div className="memory-character-cards">
        {items.map((item, index) => (
          <article key={text(item.character_id) || text(item.name) || index}>
            <strong>{text(item.name) || text(item.character_id)}</strong>
            {text(item.description) ? <p>{text(item.description)}</p> : null}
            {text(item.identity_evidence) ? (
              <small>{text(item.identity_evidence)}</small>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  )
}

export function TimelineMemoryView({
  materialId,
  payload,
  selectedSegmentId: requestedSegmentId = '',
  selectedShotId: requestedShotId = '',
  onSelectionChange,
}: {
  materialId: string
  payload: DataRecord
  selectedSegmentId?: string
  selectedShotId?: string
  onSelectionChange?: (segmentId: string, shotId?: string) => void
}) {
  const { t } = useTranslation('common')
  const segments = useMemo(() => recordsPage(payload.segments), [payload.segments])
  const [localSelectedId, setLocalSelectedId] = useState(() =>
    segments.some((item) => text(item.segment_id) === requestedSegmentId)
      ? requestedSegmentId
      : text(segments[0]?.segment_id),
  )
  const [localSelectedShotId, setLocalSelectedShotId] = useState(requestedShotId)
  const player = useRef<HTMLVideoElement>(null)
  const source = record(payload.source)
  const sourceDuration =
    numberValue(source.duration_sec) ??
    Math.max(1, ...segments.map((item) => timeRange(item.time_range).end))
  const requestedSegment = segments.find(
    (item) => text(item.segment_id) === requestedSegmentId,
  )
  const selectedId =
    requestedSegmentId && requestedSegment ? requestedSegmentId : localSelectedId
  const selected =
    segments.find((item) => text(item.segment_id) === selectedId) ?? segments[0]
  const shots = Array.isArray(selected?.shots) ? selected.shots.map(record) : []
  const selectedShotId =
    requestedSegmentId === selectedId &&
    shots.some((shot) => text(shot.shot_id) === requestedShotId)
      ? requestedShotId
      : localSelectedShotId
  const selectedShot = shots.find((shot) => text(shot.shot_id) === selectedShotId)
  const scene = record(selectedShot?.scene)
  const shotCharacters = records(selectedShot?.characters)
  const segmentCharacters = textArray(selected?.appearing_characters)
  const dialogue = records(selectedShot?.dialogue ?? selected?.dialogue_items)
  const sampledFrames = Array.isArray(selectedShot?.sampled_frame_times_sec)
    ? selectedShot.sampled_frame_times_sec.filter(
        (item): item is number => typeof item === 'number' && Number.isFinite(item),
      )
    : []

  useEffect(() => {
    if (
      !requestedSegmentId ||
      !segments.some((item) => text(item.segment_id) === requestedSegmentId)
    ) {
      return
    }
    const segment = segments.find(
      (item) => text(item.segment_id) === requestedSegmentId,
    )
    const requestedShot = records(segment?.shots).find(
      (shot) => text(shot.shot_id) === requestedShotId,
    )
    const start = requestedShot
      ? timeRange(requestedShot.time_range).start
      : timeRange(segment?.time_range).start
    if (player.current) player.current.currentTime = start
  }, [requestedSegmentId, requestedShotId, segments])

  const selectSegment = (segment: DataRecord) => {
    const segmentId = text(segment.segment_id)
    setLocalSelectedId(segmentId)
    setLocalSelectedShotId('')
    const start = timeRange(segment.time_range).start
    if (player.current) player.current.currentTime = start
    onSelectionChange?.(segmentId)
  }
  const selectShot = (shot: DataRecord) => {
    const shotId = text(shot.shot_id)
    setLocalSelectedShotId(shotId)
    const start = timeRange(shot.time_range).start
    if (player.current) player.current.currentTime = start
    if (selected) onSelectionChange?.(text(selected.segment_id), shotId)
  }
  const returnToSegment = () => {
    if (selected) selectSegment(selected)
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
          <div
            className={
              shots.length
                ? 'selection-inspector'
                : 'selection-inspector selection-inspector--no-shots'
            }
          >
            <div className="selection-inspector__header">
              <div>
                <span className="eyebrow">
                  {selectedShot ? t('materials.shots') : t('materials.segments')}
                </span>
                <h3>{text(selectedShot?.shot_id) || text(selected.segment_id)}</h3>
              </div>
              {selectedShot ? (
                <button
                  type="button"
                  className="selection-inspector__back"
                  onClick={returnToSegment}
                >
                  <ChevronLeft size={15} aria-hidden="true" />
                  {t('materials.backToSegment')}
                </button>
              ) : null}
            </div>
            {shots.length ? (
              <div
                className="shot-strip"
                role="group"
                aria-label={t('materials.shots')}
              >
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
            <div className="selection-inspector__details">
              <p>
                {text(selectedShot?.visual_description) ||
                  text(selected.segment_summary) ||
                  text(selected.narrative_function)}
              </p>
              <DetailChips
                items={values(
                  text(selectedShot?.content_type) || text(selected.content_type),
                  text(selectedShot?.narrative_function) ||
                    text(selected.narrative_function),
                  text(selectedShot?.emotional_tone) || text(selected.emotional_tone),
                  numberValue(
                    selectedShot?.emotional_intensity ?? selected.emotional_intensity,
                  ) === null
                    ? null
                    : t('materials.emotionalIntensityValue', {
                        value: numberValue(
                          selectedShot?.emotional_intensity ??
                            selected.emotional_intensity,
                        ),
                      }),
                  text(selectedShot?.dominant_action),
                  selectedShot
                    ? null
                    : values(text(selected.speech_mode), text(selected.timeline_role)),
                )}
              />
              {!selectedShot && segmentCharacters.length ? (
                <section className="memory-detail-section">
                  <h4>
                    <Users size={14} aria-hidden="true" />
                    {t('materials.characters')}
                  </h4>
                  <DetailChips items={segmentCharacters} />
                </section>
              ) : null}
              {selectedShot ? <CharacterCards items={shotCharacters} /> : null}
              <DialogueCards items={dialogue} />
              {selectedShot ? (
                <>
                  <section className="memory-detail-section">
                    <h4>
                      <Camera size={14} aria-hidden="true" />
                      {t('materials.camera')}
                    </h4>
                    <DetailChips
                      items={values(
                        text(selectedShot.shot_scale),
                        text(selectedShot.camera_angle),
                        text(selectedShot.camera_movement),
                      )}
                    />
                    {text(selectedShot.composition) ? (
                      <p>{text(selectedShot.composition)}</p>
                    ) : null}
                  </section>
                  {Object.keys(scene).length ? (
                    <section className="memory-detail-section">
                      <h4>
                        <MapPin size={14} aria-hidden="true" />
                        {t('materials.scene')}
                      </h4>
                      <DetailChips
                        items={values(
                          text(scene.interior_exterior),
                          text(scene.location),
                          text(scene.time_of_day),
                          text(scene.weather),
                          text(scene.atmosphere),
                          scene.environment_lighting,
                          scene.color_palette,
                          text(scene.color_tone),
                          scene.set_details,
                        )}
                      />
                    </section>
                  ) : null}
                  {text(selectedShot.visual_evidence) || sampledFrames.length ? (
                    <section className="memory-detail-section">
                      <h4>
                        <Eye size={14} aria-hidden="true" />
                        {t('materials.visualEvidence')}
                      </h4>
                      {text(selectedShot.visual_evidence) ? (
                        <p>{text(selectedShot.visual_evidence)}</p>
                      ) : null}
                      {sampledFrames.length ? (
                        <div>
                          <small>{t('materials.sampledFrames')}</small>
                          <DetailChips items={sampledFrames.map(clock)} />
                        </div>
                      ) : null}
                    </section>
                  ) : null}
                  {text(selectedShot.visual_annotation_status) ===
                  'provider_rejected' ? (
                    <p className="field-error">
                      {text(selectedShot.visual_annotation_failure) ||
                        t('materials.providerRejected')}
                    </p>
                  ) : null}
                </>
              ) : null}
            </div>
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

export function StoryMemoryView({
  payload,
  onOpenTimelineSegment,
}: {
  payload: DataRecord
  onOpenTimelineSegment?: (segmentId: string) => void
}) {
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
              {textArray(beat.source_segment_ids).length ? (
                <footer className="story-evidence">
                  <span>{t('materials.sourceEvidence')}</span>
                  {textArray(beat.source_segment_ids).map((segmentId) => (
                    <button
                      type="button"
                      key={segmentId}
                      onClick={() => onOpenTimelineSegment?.(segmentId)}
                    >
                      {segmentId}
                    </button>
                  ))}
                </footer>
              ) : null}
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
              {textArray(arc.key_segment_ids).length ? (
                <div className="story-evidence">
                  <span>{t('materials.sourceEvidence')}</span>
                  {textArray(arc.key_segment_ids).map((segmentId) => (
                    <button
                      type="button"
                      key={segmentId}
                      onClick={() => onOpenTimelineSegment?.(segmentId)}
                    >
                      {segmentId}
                    </button>
                  ))}
                </div>
              ) : null}
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

export function DialogueMemoryView({
  materialId,
  payload,
}: {
  materialId: string
  payload: DataRecord
}) {
  const { t } = useTranslation('common')
  const [query, setQuery] = useState('')
  const [speaker, setSpeaker] = useState('')
  const [activeSentenceId, setActiveSentenceId] = useState('')
  const player = useRef<HTMLVideoElement>(null)
  const sentenceElements = useRef(new Map<string, HTMLButtonElement>())
  const sentences = useMemo(() => recordsPage(payload.sentences), [payload.sentences])
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
  const sentenceId = (sentence: DataRecord) =>
    String(
      sentence.sentence_id ??
        `${text(sentence.start)}-${text(sentence.end)}-${text(sentence.text)}`,
    )
  const seekSentence = (sentence: DataRecord) => {
    const range = dialogueRange(sentence)
    if (player.current) player.current.currentTime = range.start
    setActiveSentenceId(sentenceId(sentence))
  }
  const followPlayback = (currentTime: number) => {
    const activeIndex = sentences.findIndex((sentence) => {
      const range = dialogueRange(sentence)
      return currentTime >= range.start && currentTime < range.end
    })
    if (activeIndex < 0) {
      setActiveSentenceId('')
      return
    }
    const id = sentenceId(sentences[activeIndex])
    setActiveSentenceId(id)
    sentenceElements.current.get(id)?.scrollIntoView?.({
      block: 'nearest',
      behavior: 'smooth',
    })
  }
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
          {t('materials.loadedCount', {
            loaded: filtered.length,
            total: pageTotal(payload.sentences),
          })}
        </span>
      </div>
      <div className="dialogue-memory__content">
        <section className="dialogue-memory__player">
          {/* The source endpoint exposes the original audio but no public WebVTT track. */}
          {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
          <video
            ref={player}
            controls
            preload="metadata"
            src={`/api/materials/${encodeURIComponent(materialId)}/source`}
            onTimeUpdate={(event) => followPlayback(event.currentTarget.currentTime)}
          />
          <p>{t('materials.dialoguePlaybackHelp')}</p>
        </section>
        <div className="dialogue-list" aria-live="polite">
          {filtered.map((sentence) => {
            const id = sentenceId(sentence)
            return (
              <button
                type="button"
                key={id}
                ref={(element) => {
                  if (element) sentenceElements.current.set(id, element)
                  else sentenceElements.current.delete(id)
                }}
                className={
                  id === activeSentenceId
                    ? 'dialogue-item dialogue-item--active'
                    : 'dialogue-item'
                }
                aria-pressed={id === activeSentenceId}
                onClick={() => seekSentence(sentence)}
              >
                <time>
                  {text(sentence.start)} — {text(sentence.end)}
                </time>
                <strong>{text(sentence.speaker) || t('common.unknown')}</strong>
                <p>{text(sentence.text)}</p>
              </button>
            )
          })}
        </div>
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
  const { t, i18n } = useTranslation('common')
  const duration = numberValue(payload.source_duration_sec) ?? 1
  const sections = recordsPage(payload.sections)
  const energy = recordsPage(payload.energy_curve)
  const beats = numericItems(payload.beats_sec)
  const accents = numericItems(payload.accents_sec)
  const [playhead, setPlayhead] = useState(0)
  const player = useRef<HTMLAudioElement>(null)
  const seek = (time: number) => {
    const bounded = Math.max(0, Math.min(duration, time))
    if (player.current) player.current.currentTime = bounded
    setPlayhead(bounded)
  }
  return (
    <div className="music-structure">
      {/* Music Material Memory contains no speech track, so a caption track is not applicable. */}
      {/* eslint-disable-next-line jsx-a11y/media-has-caption */}
      <audio
        ref={player}
        className="structure-audio"
        controls
        preload="metadata"
        src={`/api/materials/${encodeURIComponent(materialId)}/source`}
        onTimeUpdate={(event) => setPlayhead(event.currentTarget.currentTime)}
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
          <strong>{pageTotal(payload.beats_sec)}</strong>
        </article>
      </div>
      <section className="music-timeline" aria-label={t('materials.musicTimeline')}>
        <header>
          <h3>{t('materials.musicTimeline')}</h3>
          <time>{clock(playhead)}</time>
        </header>
        <div className="music-timeline__canvas">
          <i
            className="music-timeline__playhead"
            style={{ left: `${(playhead / duration) * 100}%` }}
            aria-hidden="true"
          />
          <div className="music-timeline__row music-timeline__row--sections">
            <span>{t('materials.sections')}</span>
            <div>
              {sections.map((section, index) => {
                const start = numberValue(section.start_sec) ?? 0
                const end = numberValue(section.end_sec) ?? start
                return (
                  <button
                    type="button"
                    key={text(section.section_id) || index}
                    style={{
                      left: `${(start / duration) * 100}%`,
                      width: `${Math.max(0.4, ((end - start) / duration) * 100)}%`,
                    }}
                    onClick={() => seek(start)}
                    aria-label={t('materials.seekSection', {
                      section: text(section.role) || text(section.section_id),
                      time: clock(start),
                    })}
                  >
                    {text(section.role) || text(section.section_id)}
                  </button>
                )
              })}
            </div>
          </div>
          <div className="music-timeline__row music-timeline__row--beats">
            <span>{t('materials.beats')}</span>
            <div>
              {beats.map((beat, index) => (
                <button
                  type="button"
                  key={`${beat}-${index}`}
                  style={{ left: `${(beat / duration) * 100}%` }}
                  onClick={() => seek(beat)}
                  aria-label={t('materials.seekBeat', { time: clock(beat) })}
                />
              ))}
            </div>
          </div>
          <div className="music-timeline__row music-timeline__row--accents">
            <span>{t('materials.accents')}</span>
            <div>
              {accents.map((accent, index) => (
                <button
                  type="button"
                  key={`${accent}-${index}`}
                  style={{ left: `${(accent / duration) * 100}%` }}
                  onClick={() => seek(accent)}
                  aria-label={t('materials.seekAccent', { time: clock(accent) })}
                />
              ))}
            </div>
          </div>
        </div>
        <footer>
          <span>00:00</span>
          <span>
            {t('materials.loadedLayerCount', {
              loaded: beats.length,
              total: pageTotal(payload.beats_sec),
              layer: t('materials.beats'),
            })}
          </span>
          <span>
            {t('materials.loadedLayerCount', {
              loaded: accents.length,
              total: pageTotal(payload.accents_sec),
              layer: t('materials.accents'),
            })}
          </span>
          <span>{clock(duration)}</span>
        </footer>
      </section>
      <section className="energy-panel">
        <header>
          <h3>{t('materials.energy')}</h3>
          <span>
            {t('materials.loadedCount', {
              loaded: energy.length,
              total: pageTotal(payload.energy_curve),
            })}
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
                  <button type="button" onClick={() => seek(start)}>
                    {text(section.role) || text(section.section_id)}
                  </button>
                  <time>
                    {clock(start)} — {clock(end)}
                  </time>
                </header>
                <p>
                  {text(section.energy_trend)} ·{' '}
                  {numberValue(section.mean_energy)?.toFixed(2)}
                </p>
                {Array.isArray(section.suggested_clip_duration_sec) &&
                section.suggested_clip_duration_sec.length >= 2 ? (
                  <small>
                    {t('materials.suggestedClipRange', {
                      min: `${formatNumber(
                        numberValue(section.suggested_clip_duration_sec[0]) ?? 0,
                        i18n.language,
                      )}s`,
                      max: `${formatNumber(
                        numberValue(section.suggested_clip_duration_sec[1]) ?? 0,
                        i18n.language,
                      )}s`,
                    })}
                  </small>
                ) : null}
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
  selectedSegmentId,
  selectedShotId,
  onTimelineSelectionChange,
  onOpenTimelineSegment,
}: {
  type: 'video' | 'music'
  tab: string
  materialId: string
  payload: DataRecord
  selectedSegmentId?: string
  selectedShotId?: string
  onTimelineSelectionChange?: (segmentId: string, shotId?: string) => void
  onOpenTimelineSegment?: (segmentId: string) => void
}) {
  if (type === 'video' && tab === 'timeline')
    return (
      <TimelineMemoryView
        materialId={materialId}
        payload={payload}
        selectedSegmentId={selectedSegmentId}
        selectedShotId={selectedShotId}
        onSelectionChange={onTimelineSelectionChange}
      />
    )
  if (type === 'video' && tab === 'story')
    return (
      <StoryMemoryView
        payload={payload}
        onOpenTimelineSegment={onOpenTimelineSegment}
      />
    )
  if (type === 'video' && tab === 'dialogue')
    return <DialogueMemoryView materialId={materialId} payload={payload} />
  if (type === 'music' && tab === 'structure')
    return <MusicStructureView materialId={materialId} payload={payload} />
  return <TechnicalMemoryView payload={payload} />
}
