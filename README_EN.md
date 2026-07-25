# CutMaster

<p align="center">
  <a href="README.md"><kbd>中文</kbd></a>
  &nbsp;|&nbsp;
  <a href="README_EN.md"><kbd>English</kbd></a>
</p>

<p align="center">
  <img src="assets/cutmaster_pipeline.png" alt="CutMaster method overview" width="100%">
</p>

<p align="center"><em>CutMaster: An agentic workflow for beat-aware long-video montage generation</em></p>

CutMaster is a backend-only agentic workflow for turning one long source video, one BGM
track, and a natural-language instruction into a frame-accurate music montage.
It extracts and extends the production flow used by the Mashup-Benchmark
NarratoAI adapter as an independent Python project.

CutMaster currently performs reusable full-video Shot/Segment analysis,
LLM-assisted dialogue reconstruction, structured music analysis, abstract
edit-slot planning, structured-video multi-candidate retrieval, temporally
dependent Beam Search, versioned script patching,
source-window refinement against visual cuts, and deterministic FFmpeg
rendering. Source audio is muted in the final video; only the selected BGM is
retained.

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
  -> analyze BGM beats, accents, energy curves, and sections into a structured profile
  -> let the LLM plan abstract edit slots without source timestamps
  -> globally adjust slot durations so every output boundary lands on a music accent
  -> retrieve several Shot-grounded source candidates for every slot
  -> measure candidate motion directly from the source video
  -> compute both an independently best path and a temporally dependent Beam Search path
  -> let the LLM review and patch the script only within the existing candidate pool
  -> detect internal source cuts in every candidate window in parallel
       - discard near-duplicate frames before adaptive scene detection
       - preserve original source timestamps for every retained frame
       - search each source window up to 2 seconds forward
       - minimize the worst distance from retained internal cuts to BGM beats
       - prefer at least 1 second between internal cuts and clip boundaries
  -> render every clip to an exact output-frame count
  -> concatenate normalized video-only clips
  -> loop and fade the BGM, keeping source audio muted
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
Shot calls. JSON histories and checkpoints use atomic replacement.

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
evidence. `analysis_history.json` records context snapshots, prompts, frame
labels, responses, and validation results.

### Music profiling and abstract planning

CutMaster uses `librosa` to measure RMS, onset strength, spectral centroid, and
beats, producing a unified `music_profile.json`. The profile includes an energy
series, beats, strong accents, section boundaries and roles, and
energy-dependent suggested clip-duration ranges. Beats and accents are repeated
when the BGM will loop in the final render.

Initial planning enables model thinking and requests
`ceil(target_duration / target_shot_length)` slots by default. The model
describes each slot's content, narrative role, target emotional intensity,
target kinetic energy, continuity requirement, and desired duration. It is not
allowed to produce source timestamps. A global dynamic program then adjusts all
boundaries together so cuts land on music accents while remaining monotonic and
non-empty.

### Candidate retrieval, path selection, and patching

The retrieval model grounds each slot in `video_description.json` and returns
several structured source candidates. Every candidate must:

- contain consecutive source Shots;
- start and end exactly on Shot boundaries;
- be long enough for its slot;
- contain a non-empty Shot-grounded visual description;
- use only Segments and Shots exposed in the current retrieval round.

Candidate unary scores combine semantic relevance, emotion match, motion match,
salience, and duration feasibility. CutMaster retains both:

- an independently best path formed by the top candidate for each slot;
- a global Beam Search path that also scores adjacent continuity, source-time
  relations, and changes in musical energy.

The review LLM may only issue `keep/replace` patches using existing
`candidate_id` values; it cannot invent timestamps. `planning_history.json`
records selected context, every model call and status, structured results,
script versions, and patches. Complete API, parse, and validation transactions
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
cp config.example.toml config.toml
```

`config.toml` is ignored by Git. The loader accepts either a key stored directly
as `api_key` or the name of an environment variable stored as `api_key_env`.
For environment-based configuration, use the following entry in `[llm]`,
`[vlm]`, and `[asr]`:

```toml
api_key_env = "DASHSCOPE_API_KEY"
```

Then export the key before running CutMaster:

```bash
export DASHSCOPE_API_KEY="..."
```

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
| `replan_max_rounds` | Maximum replans after retrieval or chronology failure | `3` |

### Stage 4: `[candidate_retrieval]`

| Key | Purpose | Default |
| --- | --- | --- |
| `candidates_per_slot` | Requested source candidates per Slot | `3` |
| `retrieval_batch_size` | Slots included in one request | `5` |
| `retrieval_max_rounds` | Maximum candidate-expansion rounds | `3` |
| `visual_sample_frames` | Candidate visual-validation frames | `4` |
| `protagonist_visibility_threshold` | Minimum normalized subject visibility | `0.55` |
| `protagonist_visibility_fallback_threshold` | Exhaustive-retrieval identity fallback | `0.5` |
| `motion_sample_fps` | Candidate motion sampling rate | `2.0` |
| `motion_workers` | Candidate motion decoding workers | `4` |

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
| `--target-shot-length SEC` | no | Nominal clip duration; default `4` |
| `--prompt-type TYPE` | no | Metadata supplied to script generation; default `event` |
| `--video-title TEXT` | no | Human-readable source title supplied to the model |
| `--custom-clips N` | no | Override the calculated clip count |
| `--max-clip-duration SEC` | no | Hard cap applied during duration adaptation |
| `--overwrite` | no | Replace an existing run output |

An existing `output.mp4` causes the run to stop unless `--overwrite` is passed.
`--overwrite` rebuilds the current edit task only. Complete material-analysis
caches and valid stage checkpoints with the same input signature are still reused.

## Output artifacts

Each material-analysis cache directory contains:

| Path | Contents |
| --- | --- |
| `shots.json` | Full-video PySceneDetect Shot boundaries |
| `source.srt`, `dialogues.json`, `dialogue_merged.srt` | ASR and reconstructed dialogue |
| `segment_boundaries.json` | Dialogue groups, silent gaps, and Segment/Shot membership |
| `segments/segment_XXXX.mp4` | Independently saved Segment video files |
| `video_description.json` | Structured Segment, Shot, scene, character, and dialogue descriptions |
| `analysis_history.json` | Material-analysis LLM/VLM contexts and responses |
| `analysis_manifest.json` | Cache input, model, schema, and detector signature |

Each task output directory contains:

| Path | Contents |
| --- | --- |
| `source.srt`, `dialogues.json`, `dialogue_merged.srt` | Task audit copies from the material cache |
| `music_profile.json` | Music energy, beats, accents, sections, and suggested durations |
| `edit_plan.json` | Accent-aligned abstract edit slots without source timestamps |
| `candidate_pool.json` | Structured-video candidates, model scores, and local motion features |
| `selection_diagnostics.json` | Independent-best and Beam Search paths with scores |
| `planning_history.json` | Planning-stage workflow context, model calls, and versioned scripts/patches |
| `script_raw.json` | Final selected path with slot and candidate IDs |
| `script_adapted.json` | Frame-grid output ranges, beat alignment, refined source ranges, and cut diagnostics |
| `clips/clip_XXXX.mp4` | Normalized, video-only intermediate clips |
| `montage.mp4` | Concatenated video-only montage before BGM mixing |
| `output.mp4` | Final montage with looped/faded BGM and muted source audio |
| `result.json` | Final paths, durations, clip counts, wall time, and per-stage timings |
| `cutmaster.log` | INFO/DEBUG backend execution log |

Each `script_adapted.json` item adds:

- `output_timestamp` and `output_frame_range`;
- the final refined source `timestamp`;
- detected source/output cut timestamps;
- initial and optimized maximum beat distance;
- source shift, edge-clearance fallback level, and effective clearance.

## Package layout

- `asr.py`: audio extraction, DashScope upload, asynchronous Fun-ASR polling,
  diarized subtitle conversion, and ASR reuse.
- `dialogue.py`: candidate passage construction, parallel LLM boundary
  selection, sentence reconstruction, and cue-anchor preservation.
- `llm.py`: OpenAI-compatible client and full JSON transaction retries.
- `beats.py`: librosa onset-envelope and dynamic-programming beat tracking.
- `music.py`: music energy, beats, accents, sections, and dynamic clip-duration
  analysis.
- `video_description.py`: strict Segment, Shot, scene, character, and dialogue
  data contracts.
- `prompting/`: the analyser/planner Prompt registry and executable JSON
  response contracts used to generate response templates, validate structure,
  and version model-call caches.
- `analyser.py`: full-video Shot detection, dialogue Segment assembly,
  source splitting, parallel single-Shot VLM annotation, and material caching.
- `workflow_context.py`: shared analyser/planner artifacts, model-call history,
  checkpoints, and script-version persistence.
- `planner.py`: planning facade that exposes and coordinates four decoupled stages.
- `slot_planner.py`: abstract Slot planning from the request, music profile, and
  structured source material.
- `candidate_retriever.py`: candidate retrieval and visual subject/content grounding.
- `sequence_selector.py`: pairwise-score precomputation and chronological Beam Search.
- `script_reviewer.py`: candidate-constrained review and script patching.
- `script.py`: selected-candidate duration adaptation, output-timeline
  validation, and frame-grid quantization.
- `cuts.py`: duplicate-frame-aware PySceneDetect analysis and parallel,
  forward-only frame-level minimax source-window refinement.
- `renderer.py`: encoder selection, frame-exact clip rendering, concatenation,
  and final AAC BGM mixing.
- `orchestrator.py`: end-to-end agentic orchestration, validation, timing, and result output.
- `cli.py`: command-line entry point.

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
