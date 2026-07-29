# CutMaster

<p align="center">
  <a href="README.md"><kbd>中文</kbd></a>
  &nbsp;|&nbsp;
  <a href="README_EN.md"><kbd>English</kbd></a>
</p>

<p align="center">
  <img src="assets/cutmaster_pipeline.png" alt="CutMaster method overview" width="100%">
</p>

<p align="center"><em>CutMaster: A MASTER multi-agent framework for narrative-, emotion-, and visual-aware video editing</em></p>

CutMaster is a backend-only framework that organizes a **MASTER Editing Team**
to turn one long source video, one BGM track, and a natural-language instruction
into a frame-accurate music montage.
It extracts and extends the production flow used by the Mashup-Benchmark
NarratoAI adapter as an independent Python project.

CutMaster currently performs reusable full-video Shot/Segment analysis,
LLM-assisted dialogue reconstruction, structured music analysis, abstract
edit-slot planning, original-dialogue anchoring, structured-video
multi-candidate retrieval, temporally dependent Beam Search, versioned script
patching, source-window refinement against visual cuts, and deterministic
FFmpeg rendering. Ordinary clips remain muted; only selected dialogue anchors
contribute source audio.

```text
M     = Material Analyst
ASTER = Arrangement Architect
        Story Editor
        Timeline Scout
        Edit Composer
        Revision Editor
```

M builds edit-independent Material Memory. The five-agent ASTER team then
arranges pacing, anchors the story, scouts the source timeline, composes the
sequence, and performs candidate-constrained revision. Semantic agents own
editorial decisions; deterministic beat alignment, capacity checks,
chronological constraints, and Beam Search keep decisions executable and
auditable.

## Workflow

```text
source video + BGM + instruction
  -> validate inputs and protect an existing output unless --overwrite is set
  -> detect every source Shot boundary with PySceneDetect
  -> reuse a supplied/cached SRT or transcribe with DashScope Fun-ASR
  -> reconstruct complete dialogue sentences in parallel while preserving cue anchors
  -> group the complete transcript into dialogue/monologue ranges with one LLM request
  -> expand spoken ranges to Shot boundaries and fill every silent gap as a Segment
  -> save each Segment as an independent reusable video file
  -> annotate Segments in parallel and Shots serially, one Shot and five frames per VLM call
  -> write a reusable video_description.json
  -> summarize the fully annotated video into a reusable video_summary.json
  -> analyze BGM beats, accents, energy curves, and sections into a structured profile
  -> let Arrangement Architect design Slots, pacing, and emotional progression
  -> deterministically align Slot boundaries to music accents
  -> let Story Editor select a few original-dialogue Story Anchors
  -> let Timeline Scout build a validated Candidate Space for unanchored Slots
  -> measure candidate motion directly from the source video
  -> let Edit Composer lazily score transitions for surviving Beams and compose the path
  -> let Revision Editor patch the script only within the established Candidate Space
  -> detect internal source cuts in every candidate window in parallel
       - discard near-duplicate frames before adaptive scene detection
       - preserve original source timestamps for every retained frame
       - search each source window up to 2 seconds forward
       - minimize the worst distance from retained internal cuts to BGM beats
       - prefer at least 1 second between internal cuts and clip boundaries
  -> render every clip to an exact output-frame count
  -> concatenate normalized video-only clips
  -> batch-separate and cache final anchor vocals with one Demucs invocation
  -> mix separated anchor dialogue over a looped/faded BGM and duck it under speech
  -> output.mp4 + structured intermediate artifacts
```

### Dialogue reconstruction

Fun-ASR output is initially divided into short subtitle cues. CutMaster groups
adjacent cues from the same speaker into candidate passages and asks the text
model which complete passages should be merged. The requests are processed in
parallel according to `llm.max_concurrency`.

`dialogues.json` stores both the reconstructed sentence range and every original
cue-level anchor. `dialogue_merged.srt` is the sentence-level subtitle passed to
script generation. Dialogue reconstruction disables model thinking because it
is a constrained boundary-selection task.

### Reusable source-video description

Material analysis is independent of the edit instruction, BGM, and task output
directory. `material_analysis.material_cache_dir` stores a versioned cache keyed by the
video, subtitle input, model, analysis schema, and detection settings.

Material analysis uses stage-level checkpoints rather than waiting for the whole
analysis to succeed. PySceneDetect output, subtitles/dialogue, Segment boundaries,
each Segment clip, and every successful Shot VLM annotation are persisted as soon
as they complete. After a later failure or interruption, the next run validates
these artifacts and resumes at the first missing stage without repeating successful
Shot calls. Workflow-state files and checkpoints use atomic replacement.

Full-video Shot detection, Segment clipping, per-Shot VLM annotation, candidate
frame/motion processing, pairwise VLM scoring, source-window optimization, and
final clip rendering expose progress bars with completion, throughput, and ETA.
The benchmark adapter also captures them in each task's `logs/backend.log`.

PySceneDetect first extracts all Shot boundaries with the same
`AdaptiveDetector` settings used by later cut refinement. The dialogue
segmentation LLM receives the complete transcript plus each line's covering Shot
IDs. Every dialogue ID must be assigned exactly once, and no Segment boundary
may split a Shot. Python then creates silent Segments from all remaining opening,
interstitial, and ending Shots.

Each Segment is saved as an MP4 before annotation. Segments run concurrently
under `vlm.max_concurrency`; Shots inside one Segment remain serial. Every Shot
VLM call receives exactly five uniformly sampled frames and the complete
transcript as global context. Transcript text is never accepted as visual
evidence. If an extremely short Shot has fewer than five decodable sample
positions, its last successfully decoded frame is repeated. Final structured
Shot annotations are stored independently under
`shot_annotations/`. `analysis_history.json` contains only lightweight workflow
state and does not record model calls, full prompts, context snapshots, or raw
responses.

After every Shot and Segment is annotated, the text model writes
`video_summary.json`, covering the coherent plot, chronological story beats,
character arcs, themes, and ending. This artifact is cached per source video.
Later planning and candidate retrieval use it instead of the complete
transcript for plot understanding. Only dialogue-anchor selection additionally
receives the exact dialogue inside the Segments selected by each Slot.

### Music profiling and abstract planning

CutMaster uses `librosa` to measure RMS, onset strength, spectral centroid, and
beats, producing a unified `music_profile.json`. The profile includes an energy
series, beats, strong accents, section boundaries and roles, and
energy-dependent suggested clip-duration ranges. Beats and accents are repeated
when the BGM will loop in the final render.

Initial planning enables model thinking and lets the model choose the Slot
count. `slot_planning.target_clip_duration_sec` is a soft duration target for
ordinary continuous clips. Every `narrative_role` may be repeated or omitted;
the labels do not impose a five-act template. The model describes each Slot's
content, narrative role, target emotional intensity, target kinetic energy,
continuity requirement, and desired duration. It is not allowed to produce
source timestamps. A global dynamic program then adjusts all boundaries
together so cuts land on music accents while remaining monotonic and non-empty.

### Candidate retrieval, path selection, and patching

The retrieval model grounds each Slot in `video_summary.json` plus
dialogue-stripped Segment and Shot visual descriptions, then returns several
structured source candidates. Each LLM request contains exactly one Slot, while
requests for different Slots run concurrently up to
`llm.max_concurrency`. Before calling the model, Python computes how many
non-overlapping fixed-duration windows fit in the current Segment scope. If the
scope cannot satisfy the missing candidate count, no LLM request is made and
only that Slot advances to the next, wider Segment-search round. Candidate VLM
validation is also concurrent per Slot and bounded by `vlm.max_concurrency`.
Every candidate must:

- equal the Slot's `planned_duration_sec` within millisecond timecode precision;
- remain fully inside the Segment timeline exposed in the current round;
- not overlap another candidate or any previously retained or rejected window
  for the same Slot;
- allow starts and ends inside a Shot, with overlapping Shot IDs derived by
  Python from the timestamp;
- contain a non-empty Shot-grounded visual description;
- use only Segment and Shot descriptions exposed in the current retrieval round.

Candidate unary scores combine semantic relevance, emotion match, motion match,
salience, and duration feasibility. CutMaster retains both:

- an independently best path formed by the top candidate for each slot;
- a global Beam Search path that also scores adjacent continuity, source-time
  relations, and changes in musical energy.

The review LLM may only issue `keep/replace` patches using existing
`candidate_id` values; it cannot invent timestamps. `planning_history.json`
stores planning artifacts, script versions, and patches without model-call
payloads. Complete API, parse, and validation transactions
are retried with exponential backoff, and partial results are never accepted.

### Visual-cut refinement

For each selected source range, CutMaster detects internal visual cuts over the
range plus a two-second forward search margin. Detection uses PySceneDetect's
`AdaptiveDetector` with these current defaults:

- adaptive threshold: `2.0`;
- minimum content value: `15.0`;
- minimum scene length: `0.25s`;
- near-duplicate frame threshold: grayscale mean absolute difference `< 1.0`.

The duplicate-frame filter is important for 50/60 fps material produced from a
lower frame rate. Without it, alternating duplicate/new frames can make the
adaptive detector treat ordinary motion as dozens of false cuts. Filtering only
changes which frames participate in detection; all retained frames keep their
original source timecodes.

Candidate starts are evaluated on the source-video frame grid from the original
start through `+2s`. The minimax objective first minimizes the largest distance
between any internal output cut and its nearest BGM beat, then prefers the
smallest forward shift and fewer internal cuts.

The preferred edge clearance is `1.0s`. If no candidate window is feasible,
CutMaster tries `0.75s`, `0.5s`, `0.25s`, and finally `0.0s`. Any relaxation is
recorded in `script_adapted.json` and emitted as a warning. The `0.0s` tier is a
last-resort completion path, not a normal target.

### Frame-exact rendering

Every adapted clip has an `output_frame_range`. FFmpeg renders exactly that many
frames at the configured resolution and FPS, without source audio. Clips are
concatenated without changing the planned timeline, then the BGM is looped,
trimmed to the exact montage duration, faded out, and encoded as AAC.

Encoder selection with `render.encoder = "auto"` is:

1. `h264_videotoolbox` on macOS when available;
2. `h264_nvenc` when available;
3. `libx264` otherwise.

## Requirements

- Python `3.12` (`>=3.12,<3.13`)
- `uv`
- FFmpeg with `ffmpeg` and `ffprobe` on `PATH`
- a DashScope API key for the default LLM and Fun-ASR configuration

Python 3.13 is intentionally excluded because the librosa/Numba beat-tracking
path used here is not stable in that environment.

## Installation

```bash
uv sync
cp .env.example .env
```

Put the real credential in `.env`:

```dotenv
DASHSCOPE_API_KEY="..."
```

The `[llm]`, `[vlm]`, and `[asr]` tables in `config.toml` all reference it:

```toml
api_key_env = "DASHSCOPE_API_KEY"
```

At every CLI startup, CutMaster loads `.env` from the directory containing the
selected `config.toml` without overriding variables already present in the
process environment. This also works when the benchmark launches CutMaster
from another working directory. `config.toml` is the single version-controlled
workflow configuration; `.env` is ignored by Git and stores credentials only.

## Configuration

The TOML tables follow workflow execution order. Stages without user-tunable
settings are represented by comments rather than empty tables.

### Stage 0a/0b: `[llm]` and `[vlm]`

The tables have identical but independent fields. `[llm]` handles dialogue
reconstruction, Segment grouping, Slot planning, candidate retrieval, and script
review. `[vlm]` handles per-Shot annotation, candidate visual grounding, and
pairwise continuity scoring.

| Key | Purpose | Default in example |
| --- | --- | --- |
| `model` | OpenAI-compatible text or vision-language model | `qwen3.7-plus` |
| `base_url` | OpenAI-compatible API base URL | DashScope compatible-mode URL |
| `api_key` / `api_key_env` | Direct credential or environment-variable name | placeholder |
| `enable_thinking` | Enable thinking for every request sent to this model | `true` |
| `temperature` | Sampling temperature | `0.1` |
| `max_tokens` | Maximum completion tokens | `4000` |
| `timeout_sec` | Timeout for one model request | `180` |
| `max_retries` | Retries after the first request | `3` |
| `max_concurrency` | Maximum concurrent requests for this model service | `4` |

The OpenAI SDK's own retries are disabled. CutMaster owns the full
request/parse/validate retry cycle, so `max_retries = 3` means at most four
complete attempts with `1s`, `2s`, and `4s` delays.

### Stage 1: material analysis

`[material_analysis]`

| Key | Purpose | Default |
| --- | --- | --- |
| `material_cache_dir` | Reusable material-analysis root | `.cutmaster/materials` |

`[shot_detection]`

| Key | Purpose | Default |
| --- | --- | --- |
| `adaptive_threshold` | PySceneDetect adaptive threshold | `2.0` |
| `adaptive_min_content_val` | Minimum content-change value | `15.0` |
| `adaptive_min_scene_len_sec` | Minimum Shot duration | `0.25` |
| `duplicate_frame_threshold` | Near-duplicate frame threshold | `1.0` |

The same detector settings drive full-video analysis and final source-window
optimization.

`[asr]`

| Key | Purpose | Default |
| --- | --- | --- |
| `backend` | ASR backend; currently only `bailian` | `bailian` |
| `api_key` / `api_key_env` | Direct credential or environment-variable name | placeholder |
| `reuse` | Reuse non-empty ASR artifacts | `true` |
| `timeout_sec` | Overall asynchronous ASR timeout | `1800` |
| `poll_interval_sec` | ASR polling interval | `2` |
| `max_chars` | Preferred maximum characters per cue | `20` |
| `max_subtitle_duration_sec` | Preferred maximum cue duration | `3.5` |

`[shot_annotation]`

| Key | Purpose | Default |
| --- | --- | --- |
| `shot_sample_frames` | Frames per single-Shot VLM request; fixed at five | `5` |

### Stage 3: `[slot_planning]`

| Key | Purpose | Default |
| --- | --- | --- |
| `target_clip_duration_sec` | Soft duration target for ordinary continuous clips; does not fix Slot count | `4.0` |
| `replan_max_rounds` | Maximum replans after retrieval or chronology failure | `3` |

### Stage 4: `[dialogue_anchors]`

After music alignment, the planner selects a small set of original-dialogue
anchors from each Slot's assigned Segments. An anchor may be one complete long
line or an inclusive range of consecutive dialogue items in the same Segment.
The model receives the user request, reusable story summary, complete
descriptions of the Segments selected by each Slot, relevant Shot descriptions,
and exact dialogue from only those Segments. It does not receive the music
profile or the full line-by-line transcript.
The model returns the first and last dialogue IDs; all intervening lines,
speaker changes, and natural pauses remain in the range. A Slot bounds the
corresponding source-picture passage, not the dialogue duration. Every anchor
uses a start-aligned L-cut: speech and its corresponding source picture begin
at the selected Slot start, and longer speech continues over following visual
Slots. Wherever original picture and sound coexist, they retain the same
source-time mapping. If proposed anchors conflict, Python deterministically
selects the non-overlapping subset with the most dialogue passages, breaking a
tie by the greatest total speech duration. Selection rejects
fragments, generic reactions, and lines that lack standalone meaning, narrative
importance, or direct relevance to the user's request.

| Key | Purpose | Default |
| --- | --- | --- |
| `max_anchors` | Maximum number of original-dialogue anchors | `4` |
| `min_anchor_duration_sec` | Minimum duration of a selected coherent spoken range | `1.5` |
| `enable_vocal_separation` | Separate final anchor vocals with Demucs | `true` |
| `separator_model` | Demucs model | `htdemucs` |
| `separator_device` | `auto` chooses CUDA, MPS, then CPU | `auto` |
| `separator_segment_sec` | Demucs chunk size used to bound memory | `7` |
| `separator_shifts` | Shift-ensemble count; zero is fastest | `0` |
| `separator_padding_sec` | Context added around each line before separation | `1.0` |
| `separated_loudness_lufs` | Target loudness for each separated line | `-16.0` |
| `dialogue_volume` | Selected original-dialogue volume | `1.0` |
| `bgm_duck_volume` | BGM volume during anchored dialogue | `0.08` |
| `fade_sec` | Short fade at each dialogue excerpt edge | `0.05` |

### Stage 5: `[candidate_retrieval]`

| Key | Purpose | Default |
| --- | --- | --- |
| `candidates_per_slot` | Requested source candidates per Slot | `3` |
| `retrieval_max_rounds` | Retries after the first planned-Segment search; the same count is then used for adjacent-Segment searches | `3` |
| `visual_sample_frames` | Candidate visual-validation frames | `4` |
| `protagonist_visibility_likert_threshold` | Minimum subject-visibility Likert score (1–5) | `3` |
| `motion_sample_fps` | Candidate motion sampling rate | `2.0` |
| `motion_workers` | Candidate motion decoding workers | `4` |
| `static_kinetic_energy_threshold` | Maximum mean motion energy retained as static and rejected | `0.05` |

### Stages 5–7: selection, review, and source-window optimization

| Table | Key | Purpose | Default |
| --- | --- | --- | --- |
| `[beam_search]` | `beam_width` | Paths retained by Beam Search | `8` |
| `[script_review]` | `review_rounds` | Candidate-constrained review rounds | `1` |
| `[source_window_optimization]` | `search_margin_sec` | Forward source-window search | `2.0` |
| `[source_window_optimization]` | `min_boundary_distance_sec` | Preferred cut-edge clearance | `1.0` |
| `[source_window_optimization]` | `max_workers` | Source-window optimization workers | `8` |

### Stage 8: `[render]`

| Key | Purpose | Default |
| --- | --- | --- |
| `width`, `height` | Output canvas | `1920×1080` |
| `fps` | Output frame rate and timeline grid | `30` |
| `encoder` | FFmpeg video encoder or `auto` | `auto` |
| `threads` | libx264 encoding threads | `8` |
| `bgm_volume` | Final BGM volume multiplier | `0.3` |
| `original_volume` | Source-audio volume; frame-exact mode requires `0` | `0.0` |
| `audio_sample_rate` | Final AAC sample rate | `48000` |

Text and vision concurrency are controlled independently by `llm.max_concurrency`
and `vlm.max_concurrency`; motion decoding is controlled by
`candidate_retrieval.motion_workers`, source-window optimization by
`source_window_optimization.max_workers`, and encoding by `render.threads`.

## Usage

```bash
uv run cutmaster run \
  --video /path/to/source.mp4 \
  --audio /path/to/bgm.mp3 \
  --prompt "Create a montage of every decisive goal" \
  --output-dir outputs/demo \
  --target-duration 60 \
  --target-shot-length 4 \
  --prompt-type event
```

All `run` options:

| Option | Required | Description |
| --- | --- | --- |
| `--video PATH` | yes | Long source video |
| `--audio PATH` | yes | BGM track |
| `--prompt TEXT` | yes | Montage instruction |
| `--output-dir PATH` | yes | Artifact and output directory |
| `--config PATH` | no | TOML config; defaults to `config.toml` |
| `--subtitle PATH` | no | Existing SRT; bypasses Fun-ASR |
| `--target-duration SEC` | no | Target output duration; default `60` |
| `--target-shot-length SEC` | no | Fallback duration used during adaptation; does not constrain Slot count; default `4` |
| `--prompt-type TYPE` | no | Metadata supplied to script generation; default `event` |
| `--video-title TEXT` | no | Human-readable source title supplied to the model |
| `--max-clip-duration SEC` | no | Hard cap applied during duration adaptation |
| `--overwrite` | no | Replace an existing run output |

An existing `output.mp4` causes the run to stop unless `--overwrite` is passed.
`--overwrite` rebuilds the current edit task only. Complete material-analysis
caches and valid stage checkpoints with the same input signature are still reused.

## Output artifacts

Each material-analysis cache directory contains:

| Path | Contents |
| --- | --- |
| `shots.json` | Full-video PySceneDetect Shot boundaries; FPS comes from ffprobe |
| `source.srt`, `dialogues.json`, `dialogue_merged.srt` | ASR and reconstructed dialogue |
| `segment_boundaries.json` | Dialogue groups, silent gaps, and Segment/Shot membership |
| `segments/segment_XXXX.mp4` | Independently saved Segment video files |
| `shot_annotations/shot_XXXXX.json` | Reusable final structured annotation for one Shot |
| `video_description.json` | Structured Segment, Shot, scene, character, and dialogue descriptions |
| `video_summary.json` | Reusable plot summary, story beats, character arcs, themes, and ending |
| `analysis_history.json` | Lightweight material-analysis state without model-call content |
| `analysis_manifest.json` | Cache input, model, schema, and detector signature |

Each task output directory contains:

| Path | Contents |
| --- | --- |
| `source.srt`, `dialogues.json`, `dialogue_merged.srt` | Task audit copies from the material cache |
| `music_profile.json` | Music energy, beats, accents, sections, and suggested durations |
| `edit_plan.json` | Accent-aligned abstract edit slots without source timestamps |
| `dialogue_anchors.json` | Ordered dialogue items, speakers, Slot, source-audio, and output ranges |
| `candidate_pool.json` | Structured-video candidates, model scores, and local motion features |
| `selection_diagnostics.json` | Independent-best and Beam Search paths with scores |
| `planning_history.json` | Planning artifacts and versioned scripts/patches without model-call content |
| `planning_calls.json` | Planning call tree with every retry response in full and Prompt metadata only |
| `script_raw.json` | Final selected path with slot and candidate IDs |
| `script_adapted.json` | Frame-grid output ranges, beat alignment, refined source ranges, and cut diagnostics |
| `clips/clip_XXXX.mp4` | Normalized, video-only intermediate clips |
| `montage.mp4` | Concatenated video-only montage before BGM mixing |
| `output.mp4` | Final montage with looped/faded BGM and selected original dialogue |
| `result.json` | Final paths, durations, clip counts, wall time, and per-stage timings |
| `cutmaster.log` | INFO/DEBUG backend execution log |

Each `script_adapted.json` item adds:

- `output_timestamp` and `output_frame_range`;
- the final refined source `timestamp`;
- detected source/output cut timestamps;
- initial and optimized maximum beat distance;
- source shift, edge-clearance fallback level, and effective clearance.

## Package layout

- `cutmaster.py`: the only complete MASTER Editing Team entry point, including
  input validation, stage timing, and result assembly.
- `analyser/material_analyst.py`: the Material Analyst Agent that builds
  reusable Material Memory.
- `analyser/tools/`: ASR, dialogue reconstruction, caching, and other
  non-agent material-analysis capabilities.
- `planners/aster_team.py`: ASTER agent coordination and planning feedback loops.
- `planners/arrangement_architect.py`: Slot, pacing, emotional, and narrative
  arrangement.
- `planners/story_editor.py`: original-dialogue Story Anchor selection.
- `planners/timeline_scout.py`: candidate scouting, visual grounding, and
  motion validation.
- `planners/edit_composer.py`: chronology preflight, lazy VLM transition
  scoring, and Beam Search composition.
- `planners/revision_editor.py`: candidate-constrained final review and patching.
- `planners/tools/`: internal media, scoring, error, and feedback tools used by
  the ASTER agents.
- `prompting/`: the Material Analyst/ASTER Prompt registry and executable JSON
  response contracts.
- `planners/tools/music_analysis.py`: librosa music energy, beat, accent, and
  section analysis owned through Arrangement Architect.
- `production/`: source-window refinement, frame-exact rendering,
  concatenation, and audio assembly.
- `runtime/`: model access, planning state, observability, progress, media
  probing, and shared detection infrastructure.

## Scope and limitations

- Initial material analysis must scan, accurately split, and annotate every
  source Shot. This is expensive once, then reused for the same material.
- The current motion feature uses low-resolution frame differences as an
  activity proxy rather than dense optical flow or semantic action recognition.
- One run accepts one source video and one BGM track.
- Source audio is intentionally muted; `original_volume > 0` is rejected by the
  frame-exact renderer.
- The project does not include a UI, web queue, TTS, narration subtitles,
  stock-material search, or benchmark-specific run records.
- CutMaster does not import or require NarratoAI at runtime.

## Verification

```bash
uv run pytest
uv run python -m cutmaster --help
uv run python -m cutmaster run --help
```

## Attribution

The initial workflow is derived from the MIT-licensed NarratoAI project. See
`THIRD_PARTY_NOTICES.md` and `LICENSE`.
