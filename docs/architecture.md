# CutMaster source architecture

CutMaster has three independently callable workflow stages and one thin
orchestrator.

```text
cutmaster/
├── orchestrator.py
├── analyser/
│   ├── analyser.py
│   ├── material_analyst.py
│   └── tools/
│       ├── material_library.py
│       ├── music_analysis.py
│       ├── scene_segmenter.py
│       └── ...
├── planners/
│   ├── planners.py
│   ├── aster_team.py
│   ├── arrangement_architect.py
│   ├── story_editor.py
│   ├── timeline_scout.py
│   ├── edit_composer.py
│   ├── revision_editor.py
│   └── tools/
│       ├── plan_compiler.py
│       └── source_window_optimizer.py
├── renderer/
│   ├── renderer.py
│   ├── dialogue_audio.py
│   └── ffmpeg.py
├── prompting/
├── configuration/
├── contracts/
├── runtime/
└── timecode.py
```

## Stage contracts

```text
Analyser(video) -> analyser/analysis_result.json
Analyser(music) -> analyser/music/music_analysis_result.json
Planners        -> planners/planners_result.json
Planners        -> planners/render_plan.json
Renderer        -> renderer/render_result.json
```

`analysis_result.json` identifies the selected video Material and references
its reusable Video Material Memory. `music_analysis_result.json` does the same
for a complete music Material and its reusable Music Memory. The Planners stage consumes
both results, projects Music Memory onto the requested output duration as a
Music Profile, and makes all editorial decisions.

`render_plan.json` is the only formal handoff from Planners to Renderer. It
contains exact source ranges and output frame ranges and never contains
prepared audio paths or temporary render state. `render_result.json` describes
one concrete BGM-only or dialogue render.

## Responsibilities

- `orchestrator.py` validates and coordinates a complete run, but contains no
  analysis, editorial, or rendering implementation.
- `analyser/analyser.py` owns Material ingestion and resolution. It builds or
  reuses Video Material Memory and complete-track Music Memory.
- `analyser/tools/material_library.py` assigns public Material Names, stores
  managed source copies, and checks their internal SHA-256 fingerprints.
- `analyser/tools/scene_segmenter.py` performs Scene-VLM semantic Scene
  segmentation over the detected Shots.
- `analyser/` builds reusable Shot, Segment, dialogue, story, beat, accent,
  energy, and music-section memory. None of these outputs depends on the edit
  prompt or requested output duration.
- `planners/planners.py` consumes the two analysed Materials, coordinates ASTER,
  and owns every editorial decision.
- `planners/tools/music_analysis.py` projects existing Music Memory into the
  target-duration-specific Music Profile; it is not the owner of source music
  analysis.
- `planners/tools/plan_compiler.py` finalizes durations and the output frame
  grid.
- `planners/tools/source_window_optimizer.py` chooses source windows before the
  plan is handed to rendering.
- `renderer/` only realizes an immutable RenderPlan: it renders video, prepares
  selected dialogue, mixes audio, and manages render-local caches.
- `prompting/`, `configuration/`, `contracts/`, and `runtime/` remain shared
  infrastructure.

## Material identity and reuse

A Material's public identity is its exact Material Name. When no name is
provided, the CLI uses the source filename stem as the candidate name. Names
are unique within each Material type.

- Adding the same SHA-256 bytes again under the same candidate-name family
  reuses the existing Material and any completed analysis.
- Adding different bytes under the same candidate-name family allocates the
  next public name, such as `Film (2)` or `Film (3)`.
- Adding the same bytes under a different explicit candidate name creates a
  distinct Material with its own public identity.
- SHA-256 is an internal consistency check. It is not part of the Material
  Name, is not accepted as a CLI selector, and is not a global deduplication
  key.
- A fingerprint mismatch makes a Material inconsistent and blocks it from
  analysis, edit decision, and rendering. Source replacement is unsupported.
- Completed Material Memory is reused as one immutable result. An interrupted
  video analysis resumes only when its subtitle and analysis specification are
  unchanged, preventing incompatible checkpoints from being mixed.

`analyse --material-name` and `analyse-music --material-name` control the
candidate names used while adding raw files. `plan` and `run` accept
`--video-material` and `--music-material` to resolve already analysed inputs by
exact public names. The existing `run --video ... --audio ...` form remains
supported and implicitly adds or reuses the corresponding Materials.

## Dependency direction

```text
CLI -> Orchestrator -> Analyser / Planners / Renderer

Analyser -> Material Library -> Video Material Memory / Music Memory
Planners -> ASTERTeam -> ASTER agents -> planners tools
Planners -> Music Memory -> Music Profile
Renderer -> Planners contracts / Renderer contracts / media runtime

Renderer -X-> Analyser
Renderer -X-> Planners implementations
Renderer -X-> Prompting or model gateway
```

The Renderer must be usable with `load_renderer_config()` and no API keys.
Changing codec, resolution, BGM/dialogue mode, or audio mix may create a new
render from the same plan. Changing FPS or any edit timing requires a new plan.

## Artifact layout

```text
output_dir/
├── analyser/
│   ├── analysis_result.json
│   ├── source.srt
│   ├── dialogue_merged.srt
│   ├── dialogues.json
│   └── music/
│       ├── music_analysis_result.json
│       └── music_memory.json
├── planners/
│   ├── planners_result.json
│   ├── render_plan.json
│   ├── music_profile.json
│   └── diagnostics/
├── renderer/
│   ├── output.mp4
│   └── render_result.json
├── result.json
└── cutmaster.log
```

The run-local Analyser results identify the selected Materials; their reusable
memory lives in the configured Material Library. The root files summarize the
whole workflow. Every other artifact is owned by exactly one stage.

## Public APIs

```python
from cutmaster import Analyser, Orchestrator, Planners, Renderer
from cutmaster.contracts import (
    AnalysisRequest,
    AnalysisResult,
    MusicAnalysisRequest,
    MusicAnalysisResult,
    PlannersRequest,
    PlannersResult,
    RenderRequest,
    WorkflowRequest,
)

analysis = analyser.analyse(analysis_request)
music_analysis = analyser.analyse_music(music_analysis_request)
planners_result = planners.plan(planners_request, analysis, music_analysis)
```

`Analyser.resolve_video(name)` and `Analyser.resolve_music(name)` are the
programmatic name-based selectors. Internal agents, tools, cache paths, and
fingerprints are not compatibility entry points.
