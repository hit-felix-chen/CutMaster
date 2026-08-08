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
├── planners/
│   ├── planner.py
│   ├── plan_compiler.py
│   ├── source_window_optimizer.py
│   ├── aster_team.py
│   ├── arrangement_architect.py
│   ├── story_editor.py
│   ├── timeline_scout.py
│   ├── edit_composer.py
│   ├── revision_editor.py
│   └── tools/
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
Analyser -> analyser/analysis_result.json
Planner  -> planners/render_plan.json
Renderer -> renderer/render_result.json
```

`analysis_result.json` references the source-level Material Memory cache.
`render_plan.json` is the only formal handoff from Planner to Renderer. It
contains exact source ranges and output frame ranges and never contains
prepared audio paths or temporary render state. `render_result.json` describes
one concrete BGM-only or dialogue render.

## Responsibilities

- `orchestrator.py` validates and coordinates a complete run, but contains no
  analysis, editorial, or rendering implementation.
- `analyser/` builds reusable Shot, Segment, dialogue, and story memory.
- `planners/planner.py` coordinates ASTER and owns every editorial decision.
- `planners/plan_compiler.py` finalizes durations and the output frame grid.
- `planners/source_window_optimizer.py` chooses source windows before the plan
  is handed to rendering.
- `renderer/` only realizes an immutable RenderPlan: it renders video, prepares
  selected dialogue, mixes audio, and manages render-local caches.
- `prompting/`, `configuration/`, `contracts/`, and `runtime/` remain shared
  infrastructure.

## Dependency direction

```text
CLI -> Orchestrator -> Analyser / Planner / Renderer

Planner  -> ASTERTeam -> ASTER agents -> planner tools
Renderer -> Planning contracts / Renderer contracts / media runtime

Renderer -X-> Analyser
Renderer -X-> Planner implementations
Renderer -X-> Prompting or model gateway
```

The Renderer must be usable with `load_renderer_config()` and no API keys.
Changing codec, resolution, BGM/dialogue mode, or audio mix may create a new
render from the same plan. Changing FPS or any edit timing requires a new plan.

## Artifact layout

```text
output_dir/
├── analyser/
├── planners/
│   └── diagnostics/
├── renderer/
├── result.json
└── cutmaster.log
```

The root files summarize the whole workflow. Every other artifact is owned by
exactly one stage.

## Public APIs

```python
from cutmaster import Analyser, Orchestrator, Planner, Renderer
from cutmaster.contracts import (
    AnalysisRequest,
    PlanningRequest,
    RenderRequest,
    WorkflowRequest,
)
```

Internal agents and tools are not compatibility entry points.
