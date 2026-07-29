# CutMaster source architecture

`src/cutmaster` is organized around the MASTER Editing Team and explicit shared
boundaries.

```text
cutmaster/
├── cutmaster.py
├── analyser/
│   ├── material_analyst.py
│   └── tools/
├── planners/
│   ├── aster_team.py
│   ├── arrangement_architect.py
│   ├── story_editor.py
│   ├── timeline_scout.py
│   ├── edit_composer.py
│   ├── revision_editor.py
│   └── tools/
├── editing/
├── music/
├── prompting/
├── configuration/
├── contracts/
├── runtime/
└── timecode.py
```

## MASTER Editing Team

`CutMaster` is the only complete-workflow entry point. It coordinates:

1. **M — Material Analyst**, which builds reusable Material Memory;
2. **ASTER**, the five-agent planning team;
3. deterministic source-window adaptation, audio preparation, and rendering.

`ASTERTeam` is the only component that coordinates planning agents. The agents
do not call each other directly:

```text
Arrangement Architect
  -> Story Editor
  -> Timeline Scout
  -> Edit Composer
  -> Revision Editor
```

Candidate shortages can send targeted diagnostics from Timeline Scout back to
Arrangement Architect. If repaired Slots invalidate Story Anchors, ASTERTeam
runs Story Editor again. An infeasible composition starts a new explicit
planning revision.

## Responsibilities

- `cutmaster.py` owns complete-workflow coordination, validation, timing, and
  final result assembly.
- `analyser/material_analyst.py` is the M agent and the only public material
  analysis entry point.
- `analyser/tools/` contains ASR, dialogue reconstruction, caching, and other
  non-agent material-analysis capabilities.
- `planners/aster_team.py` coordinates the ASTER agents and their feedback
  loops.
- `planners/*.py`, excluding `aster_team.py`, each define one ASTER agent.
- `planners/tools/` contains deterministic media access, scoring, validation,
  search, and planning-feedback capabilities.
- `editing/` owns post-planning script adaptation, source-window optimization,
  FFmpeg rendering, and audio assembly.
- `music/` owns reusable beat and music-profile analysis.
- `prompting/` is the only prompt-definition and response-contract registry.
- `configuration/` defines and loads application configuration.
- `contracts/` contains data passed across workflow stages.
- `runtime/` contains model access, workflow context, observability, progress,
  media probing, JSON decoding, and shared shot-detection infrastructure.

## Dependency direction

```text
CLI -> CutMaster -> Material Analyst / ASTERTeam / Editing / Music

ASTERTeam -> ASTER agents
Agents    -> their tools / Configuration / Contracts / Prompting / Runtime
Prompting -> Contracts
Runtime   -> Configuration / Prompting
```

An agent may use tools in its own feature package, but must not import another
agent's private implementation. Cross-agent work is routed through ASTERTeam.
Shared behavior belongs in `runtime/` or a feature's `tools/`; shared data
belongs in `contracts/`.

## Public APIs

Package `__init__.py` files expose only stable team and agent entry points:

```python
from cutmaster import CutMaster
from cutmaster.analyser import MaterialAnalystAgent
from cutmaster.planners import (
    ASTERTeam,
    ArrangementArchitectAgent,
    StoryEditorAgent,
    TimelineScoutAgent,
    EditComposerAgent,
    RevisionEditorAgent,
)
```

Tools are internal and are not re-exported as compatibility aliases. Avoid
generic modules such as `common.py`, `helpers.py`, or `utils.py`; every tool
module must name its editorial or media responsibility.
