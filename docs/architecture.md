# CutMaster source architecture

`src/cutmaster` is organized around workflow components and explicit shared
boundaries.

```text
cutmaster/
├── cli.py
├── orchestrator.py
├── analyser/
├── planner/
├── editing/
├── music/
├── prompting/
├── configuration/
├── contracts/
├── runtime/
└── timecode.py
```

## Responsibilities

- `orchestrator.py` sequences the workflow. It does not implement analysis,
  planning, or rendering algorithms.
- `analyser/` turns a source video into reusable subtitle, dialogue, Shot,
  Segment, and structured-description artifacts.
- `planner/` owns slot planning, candidate retrieval, pairwise scoring,
  sequence selection, and script review.
- `editing/` owns edit-script adaptation, source-window optimization, FFmpeg
  rendering, and audio assembly.
- `music/` owns beat and music-profile analysis.
- `prompting/` is the only prompt-definition and response-contract registry.
  Prompt definitions are grouped by the workflow stage that requests them.
- `configuration/` defines and loads application configuration.
- `contracts/` contains data passed across workflow stages.
- `runtime/` contains model access, workflow context, observability, progress,
  media probing, JSON decoding, and shared shot-detection infrastructure.

## Dependency direction

```text
CLI -> Orchestrator -> Analyser / Planner / Editing / Music

Feature packages -> Configuration / Contracts / Prompting / Runtime
Prompting        -> Contracts
Runtime          -> Configuration / Prompting
```

Feature packages must not import implementation details from sibling feature
packages. Shared behavior belongs in `runtime/`, and shared data belongs in
`contracts/`.

## Public APIs

Package `__init__.py` files expose only stable workflow entry points. Tests and
internal modules that need implementation helpers import their defining module
directly; private helpers are not re-exported as compatibility aliases.

Avoid generic modules such as `common.py`, `helpers.py`, or `utils.py`. New code
should be placed in a module whose name describes its responsibility.
