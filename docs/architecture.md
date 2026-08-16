# CutMaster architecture

The current backend has three independently callable, handle-only Workflow
stages behind a framework-independent Application Layer. CLI and
Mashup-Benchmark both enter through `CutMasterApplication.direct`; complete
workflow execution is no longer owned by a separate facade.

## Implemented backend layout

```text
cutmaster/
├── application/                    # composition and real use cases
│   ├── direct/
│   ├── materials/
│   ├── projects/
│   ├── runs/
│   ├── renders/
│   ├── jobs/
│   ├── settings/
│   └── ports/
├── domain/                         # pure product values and state
├── workflow/
│   ├── analyser/                      # Material Analyst + tools
│   ├── planners/                      # ASTER Team + tools
│   ├── renderer/
│   ├── contracts/                     # handle-only v2 contracts
│   ├── ports/
│   ├── prompting/
│   └── shared/
├── adapters/
│   └── cli/
├── infrastructure/
│   ├── persistence/sqlite/            # managed state and events
│   ├── storage/local/                 # Material Catalog
│   ├── media/
│   ├── models/
│   └── observability/
├── configuration/                  # Effective Configuration
└── contracts/                      # stable direct API
```

The root package still re-exports `Analyser`, `Planners`, and `Renderer`, but
their implementations live only under `workflow/`. `__main__.py` and the
installed `cutmaster` script both delegate to `adapters.cli.main`. The earlier
root workflow facade, root CLI module, raw-path stage contracts, and generic
`runtime/` package are absent.

## Repository layout and Web expansion

**Status:** the Python backend, FastAPI adapter, React/Vite client, SPA packaging,
Project Setup, and managed ASTER planning worker are implemented. Some leaf
files in the fuller map below remain a design map for the proposed general
long-job supervisor, SSE, advanced Review/Renderer flows, and Data Root
Migration; consolidated modules need not be split merely to match every
proposed filename.

CutMaster remains a standard Python `src`-layout repository. The backend is not
wrapped in another `backend/` directory, and the React client lives in the
root-level `web/` directory rather than inside the Python package. The
Application Layer refactor groups the three existing stage implementations under
one `workflow/` boundary. `workflow` is the canonical package name because it
corresponds to the domain term CutMaster Workflow; an additional `core/` or
per-stage `services/` wrapper would add no useful boundary.

### Repository root

```text
CutMaster/
├── .github/
│   └── workflows/
│       ├── ci.yml
│       └── package.yml
├── docs/
│   ├── adr/
│   ├── assets/
│   │   └── framework.png
│   ├── design/
│   │   └── master-editing-team.md
│   ├── architecture.md
│   ├── frontend-design.md
│   ├── logging.md
│   └── prompting.md
├── src/
│   └── cutmaster/
├── tests/
├── web/                              # React/Vite client
├── .env.example
├── .gitignore
├── .python-version
├── CONTEXT.md
├── LICENSE
├── README.md
├── README_EN.md
├── THIRD_PARTY_NOTICES.md
├── config.toml
├── hatch_build.py                    # SPA packaging hook
├── pyproject.toml
└── uv.lock
```

`CONTEXT.md`, packaging metadata, legal files, the version-controlled base
configuration, and its environment template stay at the root. Documentation
images live under `docs/assets/`; they are not Web assets or runtime package
resources. Mashup-Benchmark remains a separate peer-adapter repository and is
never vendored, symlinked, or collected as part of CutMaster's tests.

### Complete Web-release Python package map

The tree below records the accepted Web-release ownership map. The implemented
backend currently uses the consolidated package structure shown at the start of
this document. The Web adapter, front-end-serving infrastructure, and
submission-triggered ASTER dispatcher/worker are now present; general
supervisor/SSE and several later feature leaves remain proposed.

```text
src/cutmaster/
├── __init__.py                         # lazy public exports
├── __main__.py                         # delegates to adapters.cli.main
├── domain/
│   ├── __init__.py
│   ├── ids.py
│   ├── errors.py
│   ├── artifacts.py
│   ├── materials.py
│   ├── projects.py
│   ├── runs.py
│   ├── edits.py
│   ├── renders.py
│   ├── attempts.py
│   ├── events.py
│   └── notifications.py
│
├── application/
│   ├── __init__.py
│   ├── cutmaster.py                    # CutMasterApplication composition root
│   ├── errors.py
│   ├── direct/
│   │   ├── __init__.py
│   │   ├── commands.py
│   │   ├── results.py
│   │   ├── artifacts.py
│   │   ├── output_targets.py
│   │   └── service.py
│   ├── materials/
│   │   ├── __init__.py
│   │   ├── commands.py
│   │   ├── queries.py
│   │   ├── views.py
│   │   └── service.py
│   ├── projects/
│   │   ├── __init__.py
│   │   ├── commands.py
│   │   ├── queries.py
│   │   ├── views.py
│   │   └── service.py
│   ├── runs/
│   │   ├── __init__.py
│   │   ├── commands.py
│   │   ├── queries.py
│   │   ├── execution.py
│   │   ├── revisions.py
│   │   ├── views.py
│   │   └── service.py
│   ├── renders/
│   │   ├── __init__.py
│   │   ├── commands.py
│   │   ├── queries.py
│   │   ├── specifications.py
│   │   ├── integrity.py
│   │   ├── execution.py
│   │   ├── views.py
│   │   └── service.py
│   ├── jobs/
│   │   ├── __init__.py
│   │   ├── commands.py
│   │   ├── queries.py
│   │   ├── executor.py
│   │   ├── handlers.py
│   │   ├── events.py
│   │   ├── notifications.py
│   │   ├── views.py
│   │   └── service.py
│   ├── settings/
│   │   ├── __init__.py
│   │   ├── commands.py
│   │   ├── queries.py
│   │   ├── migration.py
│   │   ├── views.py
│   │   └── service.py
│   └── ports/
│       ├── __init__.py
│       ├── material_catalog.py
│       ├── repositories.py
│       ├── unit_of_work.py
│       ├── artifact_store.py
│       ├── data_root.py
│       ├── job_dispatcher.py
│       ├── event_store.py
│       ├── idempotency.py
│       ├── settings_store.py
│       ├── provider_connections.py
│       ├── notification_repository.py
│       └── clock.py
│
├── workflow/
│   ├── __init__.py
│   ├── contracts/
│   │   ├── __init__.py
│   │   ├── material.py
│   │   ├── analysis.py
│   │   ├── planners.py
│   │   ├── render_plan.py
│   │   ├── rendering.py
│   │   └── video.py
│   ├── ports/
│   │   ├── __init__.py
│   │   ├── models.py
│   │   ├── asr.py
│   │   ├── media.py
│   │   ├── events.py
│   │   ├── progress.py
│   │   └── cancellation.py
│   ├── shared/
│   │   ├── __init__.py
│   │   ├── execution_context.py
│   │   ├── shot_detection.py
│   │   └── timecode.py
│   ├── analyser/
│   │   ├── __init__.py
│   │   ├── analyser.py
│   │   ├── material_analyst.py
│   │   └── tools/
│   │       ├── __init__.py
│   │       ├── analysis_cache.py
│   │       ├── dialogue.py
│   │       ├── music_memory.py
│   │       └── scene_segmenter.py
│   ├── planners/
│   │   ├── __init__.py
│   │   ├── planners.py
│   │   ├── aster_team.py
│   │   ├── arrangement_architect.py
│   │   ├── story_editor.py
│   │   ├── timeline_scout.py
│   │   ├── edit_composer.py
│   │   ├── revision_editor.py
│   │   └── tools/
│   │       ├── __init__.py
│   │       ├── errors.py
│   │       ├── music_profile.py
│   │       ├── plan_compiler.py
│   │       ├── planners_feedback.py
│   │       ├── segment_media.py
│   │       ├── source_window_optimizer.py
│   │       └── visual_scoring.py
│   ├── renderer/
│   │   ├── __init__.py
│   │   ├── renderer.py
│   │   └── tools/
│   │       ├── __init__.py
│   │       ├── dialogue_audio.py
│   │       ├── audio_mix.py
│   │       └── ffmpeg_commands.py
│   └── prompting/
│       ├── __init__.py
│       ├── core.py
│       ├── registry.py
│       ├── failure_catalog.py
│       ├── analyser/
│       │   ├── __init__.py
│       │   └── tasks.py
│       └── planners/
│           ├── __init__.py
│           └── tasks.py
│
├── adapters/
│   ├── __init__.py
│   ├── cli/
│   │   ├── __init__.py
│   │   ├── main.py
│   │   ├── parser.py
│   │   ├── direct_commands.py
│   │   ├── serve_command.py
│   │   └── presenter.py
│   └── web/                             # FastAPI adapter
│       ├── __init__.py
│       ├── app.py
│       ├── server.py
│       ├── dependencies.py
│       ├── problem_details.py
│       ├── sse.py
│       ├── static_assets.py
│       ├── schemas/
│       │   ├── __init__.py
│       │   ├── common.py
│       │   ├── problem.py
│       │   ├── materials.py
│       │   ├── projects.py
│       │   ├── runs.py
│       │   ├── edits.py
│       │   ├── renders.py
│       │   ├── jobs.py
│       │   └── settings.py
│       ├── routes/
│       │   ├── __init__.py
│       │   ├── health.py
│       │   ├── materials.py
│       │   ├── projects.py
│       │   ├── runs.py
│       │   ├── frozen_edits.py
│       │   ├── render_variants.py
│       │   ├── attempts.py
│       │   ├── activity.py
│       │   ├── notifications.py
│       │   ├── settings.py
│       │   ├── media.py
│       │   ├── events.py
│       │   └── spa.py
│
├── infrastructure/
│   ├── __init__.py
│   ├── persistence/
│   │   ├── __init__.py
│   │   └── sqlite/
│   │       ├── __init__.py
│   │       ├── database.py
│   │       ├── schema.py
│   │       ├── unit_of_work.py
│   │       ├── project_repository.py
│   │       ├── run_repository.py
│   │       ├── render_repository.py
│   │       ├── attempt_repository.py
│   │       ├── notification_repository.py
│   │       ├── event_store.py
│   │       ├── idempotency_store.py
│   │       └── migrations/
│   │           ├── __init__.py
│   │           └── v001_initial.py
│   ├── storage/
│   │   ├── __init__.py
│   │   └── local/
│   │       ├── __init__.py
│   │       ├── data_root.py
│   │       ├── material_catalog.py
│   │       ├── artifact_store.py
│   │       ├── leases.py
│   │       ├── fingerprints.py
│   │       ├── integrity.py
│   │       ├── migration.py
│   │       └── settings_store.py
│   ├── jobs/
│   │   ├── __init__.py
│   │   └── subprocess/
│   │       ├── __init__.py
│   │       ├── protocol.py
│   │       ├── dispatcher.py
│   │       ├── supervisor.py
│   │       ├── worker.py
│   │       └── cancellation.py
│   ├── models/
│   │   ├── __init__.py
│   │   ├── provider_connections.py
│   │   ├── openai_compatible.py
│   │   ├── json_codec.py
│   │   └── usage.py
│   ├── asr/
│   │   ├── __init__.py
│   │   └── dashscope.py
│   ├── media/
│   │   ├── __init__.py
│   │   ├── ffprobe.py
│   │   ├── ffmpeg.py
│   │   ├── demucs.py
│   │   └── frames.py
│   └── observability/
│       ├── __init__.py
│       ├── logging.py
│       ├── progress.py
│       └── workflow_events.py
│
├── configuration/
│   ├── __init__.py
│   ├── schema.py
│   ├── loader.py
│   ├── environment.py
│   ├── data_root.py
│   └── snapshots.py
│
└── contracts/                          # stable direct Application contracts
    ├── __init__.py
    └── workflow.py
```

Agent classes remain directly visible in their owning stage package; only
deterministic helpers belong in that stage's `tools/` directory. Root-level
public re-exports keep `from cutmaster import Analyser, Planners, Renderer`
available even though their implementation lives under `workflow/`. CLI and
Mashup-Benchmark now both call `app.direct`, so the former root `cli.py`,
complete-workflow facade, `WorkflowRequest`, and raw-path stage DTOs have been
removed. `contracts/workflow.py` remains the stable home of direct Application
commands and results. The three root stage-class exports remain supported,
while their v2 request contracts are handle-only and intentionally do not
preserve the former raw-path DTO signatures.

### React client

The React client is feature-first and page-thin. Pages own route parameters and
page composition; Features own interaction logic; TanStack Query owns server
state; React Router owns URL state; focused reducers own unsaved Creative Brief
and Revision Draft state. No Redux or Zustand store is introduced in the first
release.

```text
web/
├── package.json
├── package-lock.json
├── index.html
├── vite.config.ts
├── vitest.config.ts
├── playwright.config.ts
├── openapi-ts.config.ts
├── eslint.config.js
├── tsconfig.json
├── tsconfig.app.json
├── tsconfig.node.json
├── public/
│   ├── favicon.svg
│   └── app-icon.svg
├── src/
│   ├── main.tsx
│   ├── vite-env.d.ts
│   ├── app/
│   │   ├── App.tsx
│   │   ├── router.tsx
│   │   ├── routes.ts
│   │   ├── AppProviders.tsx
│   │   ├── query-client.ts
│   │   ├── AppErrorBoundary.tsx
│   │   └── providers/
│   │       ├── LocaleProvider.tsx
│   │       ├── ColorModeProvider.tsx
│   │       ├── QueryProvider.tsx
│   │       ├── EventStreamProvider.tsx
│   │       └── ToastProvider.tsx
│   ├── pages/
│   │   ├── SetupPage.tsx
│   │   ├── ProjectsPage.tsx
│   │   ├── MaterialsPage.tsx
│   │   ├── ActivityPage.tsx
│   │   ├── SettingsPage.tsx
│   │   ├── NotFoundPage.tsx
│   │   └── project/
│   │       ├── ProjectLayout.tsx
│   │       ├── ProjectOverviewPage.tsx   # Project Setup route component
│   │       ├── ProjectRunsPage.tsx
│   │       ├── RunDetailPage.tsx
│   │       ├── ReviewPage.tsx
│   │       └── ProjectOutputsPage.tsx
│   ├── api/
│   │   ├── http.ts
│   │   ├── commands.ts
│   │   ├── query-keys.ts
│   │   ├── event-stream.ts
│   │   ├── materials.ts
│   │   ├── projects.ts
│   │   ├── runs.ts
│   │   ├── renders.ts
│   │   ├── activity.ts
│   │   ├── notifications.ts
│   │   ├── settings.ts
│   │   ├── media.ts
│   │   └── generated/
│   │       └── schema.d.ts
│   ├── types/
│   │   ├── route-state.ts
│   │   ├── revision-draft.ts
│   │   ├── brief-draft.ts
│   │   ├── media-playback.ts
│   │   └── i18next.d.ts
│   ├── components/
│   │   ├── layout/
│   │   ├── ui/
│   │   └── media/
│   ├── features/
│   │   ├── materials/
│   │   │   └── memory/
│   │   ├── projects/
│   │   ├── creative-brief/
│   │   ├── runs/
│   │   ├── review/
│   │   ├── outputs/
│   │   ├── activity/
│   │   ├── notifications/
│   │   ├── appearance/
│   │   │   ├── AppearanceSettings.tsx
│   │   │   ├── LanguageSettings.tsx
│   │   │   └── ColorModeSettings.tsx
│   │   └── settings/
│   │       ├── ModelPresetForm.tsx
│   │       ├── ProviderConnectionCard.tsx
│   │       ├── ExecutionSettingsForm.tsx
│   │       ├── RenderSettingsForm.tsx
│   │       ├── StoragePanel.tsx
│   │       └── MoveDataRootDialog.tsx
│   ├── hooks/
│   ├── i18n/
│   │   ├── index.ts
│   │   ├── detect-locale.ts
│   │   ├── storage.ts
│   │   ├── formatters.ts
│   │   ├── problem-messages.ts
│   │   └── locales/
│   │       ├── zh-CN/
│   │       │   ├── common.json
│   │       │   ├── materials.json
│   │       │   ├── projects.json
│   │       │   ├── brief.json
│   │       │   ├── runs.json
│   │       │   ├── review.json
│   │       │   ├── outputs.json
│   │       │   ├── activity.json
│   │       │   ├── settings.json
│   │       │   └── problems.json
│   │       └── en-US/
│   │           ├── common.json
│   │           ├── materials.json
│   │           ├── projects.json
│   │           ├── brief.json
│   │           ├── runs.json
│   │           ├── review.json
│   │           ├── outputs.json
│   │           ├── activity.json
│   │           ├── settings.json
│   │           └── problems.json
│   ├── color-mode/
│   │   ├── color-mode.ts
│   │   ├── storage.ts
│   │   ├── system-preference.ts
│   │   ├── bootstrap.ts
│   │   └── apply-color-mode.ts
│   ├── styles/
│   │   ├── reset.css
│   │   ├── tokens.css
│   │   ├── semantic-colors.css
│   │   ├── modes/
│   │   │   ├── dark.css
│   │   │   └── light.css
│   │   ├── globals.css
│   │   └── accessibility.css
│   └── test/
│       ├── setup.ts
│       ├── render.tsx
│       ├── server.ts
│       ├── handlers.ts
│       └── factories.ts
└── e2e/
    ├── global-setup.ts
    ├── fixtures.ts
    ├── routing-state.spec.ts
    ├── material-library.spec.ts
    ├── project-workflow.spec.ts
    ├── review-revision.spec.ts
    ├── activity-recovery.spec.ts
    ├── appearance-localization.spec.ts
    └── settings-storage.spec.ts
```

Component tests live beside their feature implementation; `src/test/` contains
only shared test infrastructure. Localized user-facing copy is split into
feature namespaces beneath both locale directories. The current first slice
uses explicit transport types in `features/shared/api.ts`. **Proposed:** once
the FastAPI responses have concrete OpenAPI schemas, generated transport types
will live in `api/generated/`, be committed, and be checked for drift in CI;
`types/` remains reserved for browser-only route, draft, playback, and
library-augmentation types.

Vite writes ignored production assets to `web/dist/`. Release packaging maps
that directory into the following wheel-only package-resource path without
copying generated assets back into the authored Python source tree:

```text
cutmaster/adapters/web/static/          # full web/dist mapping; wheel-only
├── index.html
├── favicon.svg
├── app-icon.svg
├── .vite/
│   └── manifest.json
└── assets/                             # Vite hashed JS/CSS/media
```

The Web adapter locates either the source `web/dist` tree or the installed
package resource directory, and SPA fallback never captures `/api` routes,
including Material source streams. During frontend development, Vite serves
the SPA and proxies `/api` requests to the FastAPI development server. Running
`cutmaster serve` from an editable source
checkout without a Web build returns a clear build instruction instead of
silently serving an incomplete UI; an installed release wheel must always
contain the built SPA. `hatch_build.py` rejects a release artifact when the
expected Vite manifest or hashed assets are absent and stages them without
mutating `src/`. A release sdist carries the prebuilt SPA payload so building a
wheel from that sdist requires Python tooling but not Node.js; a release build
from a Git checkout runs the pinned npm build first.

Because `cutmaster serve` is part of the official first-release CLI, FastAPI and
Uvicorn are normal runtime dependencies rather than an optional extra. Their
imports remain inside the Web adapter so direct component commands do not
initialize the server stack.

### Complete test layout (Proposed Web additions included)

```text
tests/
├── domain/
├── application/
│   ├── direct/
│   ├── materials/
│   ├── projects/
│   ├── runs/
│   ├── renders/
│   ├── jobs/
│   └── settings/
├── workflow/
│   ├── analyser/
│   ├── planners/
│   └── renderer/
├── adapters/
│   ├── cli/
│   └── web/
├── infrastructure/
│   ├── persistence/
│   ├── storage/
│   └── jobs/
├── contracts/
│   ├── test_direct_api.py
│   ├── test_cli_compatibility.py
│   ├── test_benchmark_compatibility.py
│   ├── test_artifact_manifest.py
│   ├── test_artifact_manifest_paths.py
│   ├── test_v1_plan_boundary.py
│   ├── test_stage_v2_contracts.py
│   └── test_raw_path_stage_dtos_rejected.py
├── packaging/
│   ├── test_installed_wheel.py
│   └── test_wheel_from_sdist.py
└── system/
    └── test_local_application.py
```

Python tests mirror ownership rather than introducing a second `unit/` hierarchy.
Benchmark's own adapter integration tests remain in Mashup-Benchmark; CutMaster
tests only its public direct API and compatibility contracts.

### Local and generated paths

The following paths are intentionally absent from the tracked source structure:

```text
.cutmaster/                         # default Application Data Root
.cutmaster-location                 # custom Data Root pointer
.env                                # API keys
*.local.toml                        # sparse sibling non-secret settings
.venv/
.pytest_cache/
.ruff_cache/
outputs/                            # historical explicit output location
dist/                               # Python wheel and sdist
build/
web/node_modules/
web/dist/
web/.vite/
```

The existing `.cutmaster/materials-backup/` remains untouched and is never
scanned, imported, deleted, or copied by the new application.

### Implementation sequence

The backend migration completed these boundaries on 2026-08-12:

1. `domain/`, Application and Workflow contracts, secret-free Effective
   Configuration, and the lazy seven-service `CutMasterApplication`.
2. Material ownership in `app.materials` backed by the local ID-owned Material
   Catalog, including fingerprints, consistency checks, deletion guards, and
   leases.
3. Real synchronous component and complete-workflow operations in `app.direct`,
   including managed Direct Workflow Bundles and atomic Artifact Manifest
   publication.
4. Peer CLI and Mashup-Benchmark adapters calling `app.direct`; the installed
   console entry point is `cutmaster.adapters.cli.main:main`, and
   `cutmaster.__main__` delegates to it.
5. Analyser, Planners, Renderer, prompting, contracts, and shared helpers under
   `workflow/`, with handle-only v2 stage requests and a portable RenderPlan v2.
6. SQLite-backed Projects, Material References, Runs, Frozen Edits, Render
   Variants, Attempts, jobs, durable events, command receipts, and settings.

The next work is **Proposed**: extend the implemented ASTER subprocess path into
a general supervisor for Material Analysis, Renderer, Review, shared scheduling,
and orphan recovery; add SSE; complete Review/Render flows; and then implement
guarded Data Root Migration. OpenAPI, media Range requests, SPA packaging,
Project Setup/Run route tests, and ASTER worker tests are already implemented.
Historical output
directories and v1 plans remain
untouched; they are not compatibility inputs for the handle-only stage API.

The generic `runtime/` package was split according to dependency ownership:

| Previous module | Implemented location |
|---|---|
| `runtime/artifact_layout.py` | `application/direct/artifacts.py` |
| `runtime/model_gateway.py`, `runtime/json_codec.py` | `infrastructure/models/` |
| `runtime/observability.py` | `infrastructure/observability/` |
| `runtime/progress.py` | `infrastructure/observability/progress.py` |
| `runtime/media_probe.py` | `infrastructure/media/ffprobe.py` |
| `runtime/shot_detection.py` | `workflow/shared/shot_detection.py` |
| `runtime/workflow_context.py` | `workflow/shared/execution_context.py` |
| `timecode.py` | `workflow/shared/timecode.py` |
| `analyser/tools/asr.py` | `workflow/analyser/tools/asr.py` |
| `renderer/ffmpeg.py` | `workflow/renderer/ffmpeg.py` |
| `renderer/dialogue_audio.py` | `workflow/renderer/dialogue_audio.py` |

Workflow request contracts and cancellation/event/progress protocols live in
`workflow/contracts/` and `workflow/ports/`. Concrete model, media-probe, JSON,
logging, and terminal-progress helpers live in `infrastructure/`; CLI
presentation helpers live in `adapters/cli/`. Further provider/media port injection is
**Proposed** and is separate from the completed handle-only request migration.

### Material ownership boundary

Material identity and lifecycle are Application concerns, not Analyser tools.
`domain/materials.py` defines the Material entity; the Application
`materials` service owns add, ensure, resolve, reference checks, deletion, and
leases through the `MaterialCatalog` port; and
`infrastructure/storage/local/material_catalog.py` implements the manifest,
managed copies, fingerprints, directories, and cross-process locks.

The existing `analyser/tools/material_library.py` moves to that local
infrastructure implementation. `workflow/analyser` retains Material Analyst
intelligence and Material Memory construction/reuse, but no longer allocates
names, resolves catalog entries, copies source files, or deletes Materials.
Direct CLI use cases first resolve or ensure a Material through
`app.materials`, then invoke `app.direct.analyse` with that exact identity.

At the Workflow boundary, Analyser accepts a `MaterialRuntimeHandle` defined in
`workflow/contracts/material.py`. It contains the immutable Material ID, type,
name, fingerprint, runtime-only absolute source and memory paths, and any
verified immutable video-subtitle sidecar. The Application derives this handle
from canonical ID and Data Root-relative references while holding the Material
lease; the handle is never persisted as product state.

Raw source paths and candidate names terminate at the Application materials
use case. Analyser still verifies the managed source and fingerprint and owns
analysis compatibility decisions such as analysis version, subtitle identity,
and effective analysis specification. After success, the Application records
the returned Material Memory references through `MaterialCatalog`; Analyser
never writes the catalog manifest itself.

Planners accepts `AnalysedVideoRuntimeHandle` and
`AnalysedMusicRuntimeHandle`, which extend the exact Material identity with
validated Material Memory version, fingerprint, and resolved runtime artifact
paths. Its request also contains a transport-neutral `PlannersBrief` and
`PlannersOptions`; it contains no raw video/audio path or Material Name
selector. The Application holds both Material leases for the whole planning
operation and translates CLI or managed inputs into this contract.

`PlannersBrief` carries Editing Intent and Target Duration. CLI/Benchmark-only
compatibility controls such as target shot length, prompt type, and maximum clip
duration remain in `PlannersOptions`; they are not added to the frontend
Creative Brief. Planners revalidates that every Memory belongs to the supplied
Material ID and fingerprint before ASTER begins.

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
for a complete music Material and its reusable Music Memory. The Planners stage
consumes both results, projects Music Memory onto the requested output duration
as a Music Profile, and makes all editorial decisions.

`render_plan.json` is the only formal handoff from Planners to Renderer. It
contains exact source ranges and output frame ranges and never contains
prepared audio paths or temporary render state. `render_result.json` describes
one concrete BGM-only or dialogue render.

The implemented RenderPlan v2 schema is portable: it records the selected video and
music Material IDs and expected fingerprints, but never absolute paths, mtimes,
or prepared media. At render time the Application resolves those identities
while holding both Material leases and supplies a separate
`RenderRuntimeBindings` object containing the two `MaterialRuntimeHandle`s.

Renderer receives the immutable RenderPlan, runtime bindings, a structured
output target, and RenderOptions. It validates plan/binding IDs, fingerprints,
FPS, and the complete frame timeline before touching media. The CLI retains the
`render --plan` command and option spelling; the Application loads a supported
plan, resolves its Materials, and constructs the Renderer request. The portable
schema is v2. Refactored rendering supports v2 plans only and reports an
unsupported-plan-version error for v1 input; existing v1 plans and output
directories are left byte-for-byte unchanged and are never migrated.

ASTER agents produce editorial decisions; they do not create product records.
The deterministic Plan Compiler at the end of Planners turns those accepted
decisions into RenderPlan. For managed execution, the Application atomically
commits that plan as the sole immutable payload of a new Frozen Edit and stores
only Frozen Edit identity, ASTER Run ownership, optional parent, origin
(`initial` or `guided_revision`), and the plan's portable artifact reference in
product persistence. Timeline fields are never duplicated as independently
writable database columns. Render Variants attach to this stable edit-version
identity rather than to an ASTER execution attempt or a plan file path. Product
persistence has no mutable `current_frozen_edit_id` or `final_frozen_edit_id`;
the newest creation sequence is only a default query result for adapters.

**Proposed:** Guided Revision will not rerun ASTER. It will validate a
user-selected replacement against the existing Candidate Space and
deterministically recompile a new RenderPlan. The implemented Application
persistence can commit a derived Frozen Edit that points back to its source,
permits multiple children in one ASTER Run, and rejects cross-Run parentage;
candidate-space validation and recompilation are not wired yet. Direct CLI and
Benchmark workflows use RenderPlan without creating Frozen Edit product
history.

## Implemented ownership

- `domain/` contains pure identities, entities, values, and state transitions;
  it performs no filesystem, database, provider, or transport I/O.
- `application/` owns product use cases, Material resolution, managed history,
  output ownership, durable execution state, configuration snapshots, and Data Root
  resolution. The current local services use the SQLite Application Store and
  Material Catalog; narrower ports remain available where adapter substitution
  is already required.
- `application/cutmaster.py` is the public composition root. It lazily groups
  `direct`, `materials`, `projects`, `runs`, `renders`, `jobs`, and `settings`
  services without implementing those use cases itself.
- `workflow/analyser/` builds or reuses Video Material Memory and complete-track
  Music Memory from an already resolved Material handle. It does not allocate
  Material identities, copy library sources, or mutate the Material Catalog.
- `workflow/planners/` consumes analysed video and music handles, coordinates
  ASTER, projects Music Memory into a target-duration Music Profile, and owns all
  Planners-stage editorial decisions through deterministic tools and the five
  ASTER roles.
- `workflow/renderer/` realizes an immutable portable RenderPlan from explicit
  runtime bindings. It prepares dialogue and mix decisions but accesses media
  through the current local media helpers; broader media-port injection is
  proposed.
- `workflow/prompting/` owns prompt definitions, response contracts, and the
  shared failure catalogue. Analyser and Planners use prompt packages and model
  response contracts; Renderer imports only the transport-neutral failure
  catalogue and never invokes a prompt or model.
- `workflow/contracts/` and `workflow/ports/` contain stage-boundary data and
  inward protocols without filesystem or media I/O. `workflow/shared/` owns
  cross-stage execution support such as timecodes, shot detection, and the
  current model execution context; concrete model, media, logging, and progress
  helpers remain in `infrastructure/` pending broader port injection.
- `infrastructure/` implements SQLite persistence, local Material storage,
  model access, FFprobe helpers, and observability. The Web adapter currently
  owns the implemented ASTER subprocess dispatcher; a reusable general job
  supervisor remains **Proposed**.
- `adapters/cli/` translates transport input and output and calls only the
  Application Layer. `adapters/web/` is the implemented FastAPI peer adapter
  and owns the current ASTER dispatcher/worker; its other managed long-job and
  SSE expansion is **Proposed**.
- `configuration/` loads and validates bootstrap, local overlay, environment,
  Data Root resolution, and non-secret snapshot configuration for the
  composition root. Data Root Migration is **Proposed**.
- `contracts/workflow.py` and root stage-class re-exports remain public. The mixed-purpose
  `runtime/` package has no target equivalent and must not be recreated under
  another generic name.

## Material identity and reuse

A Material owns an opaque internal Material ID and is publicly selected by its
exact Material Name. When no name is provided, the CLI uses the source filename
stem. Names are unique within each Material type.

- A strict import rejects an occupied name; it never appends a numeric suffix,
  overwrites, or replaces the existing Material.
- The Application Material service and direct use case expose an idempotent
  ensure operation: they reuse the existing Material only when type, exact name,
  and bound SHA-256 all match. The same name with different bytes is a collision.
- Adding the same bytes under a different explicit candidate name creates a
  distinct Material with its own public identity.
- SHA-256 is an internal consistency check. It is not part of the Material
  ID or Name, is not accepted as a CLI selector, and is not a global
  deduplication key.
- Browsing projections, Material Memory inspection, and byte-range preview use
  a deletion-safe read lease and do not recompute SHA-256 for every request.
  Any operation that consumes a Material for analysis, planning, or rendering,
  plus explicit consistency verification, uses the verified lease and checks
  the complete source digest.
- Managed storage is derived only from the internal ID as
  `media/<video|music>/mat_<uuid>/`; names and fingerprints never enter paths.
- A fingerprint mismatch makes a Material inconsistent and blocks it from
  analysis, edit decision, and rendering. Source replacement is unsupported.
- Completed Material Memory is reused as one immutable result. An interrupted
  video analysis resumes only when its subtitle and analysis specification are
  unchanged, preventing incompatible checkpoints from being mixed.

`analyse --material-name` and `analyse-music --material-name` control the
names used while ensuring raw-file inputs. `plan` and `run` accept
`--video-material` and `--music-material` to resolve already analysed inputs by
exact public names. The existing `run --video ... --audio ...` form remains
supported and idempotently ensures the corresponding Materials.

## Dependency direction

The implemented product architecture uses a framework-independent Application
Layer. CLI, Mashup-Benchmark, and FastAPI Web are peer inbound adapters. None
calls another adapter. The Application Layer owns product workflows and input
resolution, while the stage services remain
independently callable public core APIs for callers that already hold valid
Application-created runtime handles. Raw files and Material Names enter through
`app.direct` or `app.materials`, not through those stage contracts.

`CutMasterApplication` is the single composition entry point used by CLI,
Benchmark, and FastAPI Web. It wires
configuration, persistence, storage, and use-case services, but contains no
use-case implementation of its own. Its public surface is grouped rather than
accumulated into one giant facade:

```python
app.direct       # analyse, analyse_music, plan, render, execute_workflow
app.materials    # managed Material use cases
app.projects     # Edit Project and Creative Brief use cases
app.runs         # ASTER Run and Guided Revision use cases
app.renders      # Render Variant use cases
app.jobs         # Execution Attempt, activity, stop, retry, and resume
app.settings     # model connections, execution settings, storage, and Data Root
```

`app.settings` currently owns Effective Configuration reads, validated local
overlay saves, and storage reporting. Model-connection UI, bulk cleanup, and
guarded Data Root Migration are **Proposed**. Browser-only Locale and Colour
Mode preferences will not pass through this service or be persisted as
Application state.

### Effective configuration

Every adapter loads one Effective Configuration through the Application
composition entry point. For the default repository setup, version-controlled
`config.toml` provides defaults and research controls, while git-ignored
`config.local.toml` is a sparse local overlay containing only non-secret values
that Settings may edit. The overlay path is derived from the exact selected base
path by inserting `.local` before its final `.toml` suffix: `config.toml` maps to
`config.local.toml`, and `/path/eval.toml` maps to
`/path/eval.local.toml`. If that sibling does not exist, only the selected base
is loaded. The loader never falls back to or mixes in a local overlay from the
repository root or current working directory.

The merge order for non-secret fields is base configuration followed by the
local overlay. API keys remain exclusively in the process environment or the
git-ignored `.env` beside the selected base configuration; an existing process
environment value wins over the dotenv file. The loader validates the complete
merged configuration, rejects implicit scalar coercion, materializes defaults,
and removes the former Material Library path before exposing the canonical
snapshot. Application configuration rejects inline `api_key` values; the
runtime `load_config()` path still materializes provider secrets after the
Application resolves their environment references. A historical Material
Library key may remain in the versioned base for that loader but is rejected in
the Application overlay and never becomes a second storage authority.
Settings likewise validates a candidate merge and atomically replaces the
local overlay only after validation, so CLI, Benchmark, workers, and Web
observe either the previous complete file or the new complete file, never a
partial write.

Research controls that the first-release Settings UI does not expose stay in
the version-controlled base. Managed ASTER Runs and Render Variants snapshot the
effective non-secret values required for reproducibility, not either source
file. Locale and Colour Mode are browser preferences and do not participate in
this merge.

Saving Settings atomically writes the local overlay and reports that an
Application restart is required. A newly opened `CutMasterApplication` sees the
new Effective Configuration; an existing instance and any synchronous Direct
Workflow keep the immutable configuration loaded at process start. Managed
ASTER Runs persist their non-secret configuration snapshot, while Render
Variants persist their normalized Render Specification.

Secrets are deliberately excluded from snapshots. Direct execution resolves
the required environment references when constructing a runtime configuration.
The managed ASTER subprocess resolves secrets at its Execution Attempt start,
allowing credential repair without changing the owning operation's non-secret
snapshot. Other managed workers will follow this policy when the proposed
general supervisor is implemented.

Inbound adapters translate only their transport-specific input and output.
The implemented CLI and Benchmark adapters obtain services from
`CutMasterApplication`; they do not construct Analyser, Planners, Renderer, or
repositories themselves. Every adapter opens the same Application context with
`CutMasterApplication.open(config_path)`, which resolves one Application Data
Root and one Material Catalog. Service groups are lazy: using `app.direct`
does not initialize SQLite, the durable queue, or unrelated model providers,
while managed groups open those resources only when accessed.

### Web adapter protocol and managed ASTER execution

The implemented FastAPI adapter exposes noun-based queries for Projects,
Materials, sanitized Material Memory, leased source-media Range streams,
Activity/events, Settings, storage, and Run details. It exposes Project Setup,
Settings, Stop Attempt, and Start editing commands through Application use
cases. Start editing returns a durable Run/Attempt/job submission and dispatches
real ASTER planning to an isolated subprocess. The proposed general supervisor
expansion adds Material Analysis, Retry/Resume, Save revision, and Render. The
adapter has no generic
`/commands` endpoint and does not expose database-shaped CRUD.
Each route maps a transport DTO to one Application use case and maps its result
or domain error back to HTTP; authorization-free local transport concerns never
enter the Application contract. Server-Sent Events are read-only projections of
durable job events and cannot be used to submit commands.

Every managed state-changing Application request carries a client-generated
UUID `command_id`. The Web adapter maps the HTTP `Idempotency-Key` header to that
field; transport retries reuse it, while each new user intent—including a new
domain Retry or Resume action—uses another ID. SQLite durably records the
command kind, canonical request digest, and result references before returning.
Replaying the same ID and digest returns the original response across process
restarts; reusing an ID with different input returns an idempotency conflict.

Asynchronous commands return HTTP `202 Accepted` with `command_id`, the created
or updated domain-object reference, and its Execution Attempt and job
references. The same IDs correlate later SSE events. UI button disabling is a
usability guard, not the correctness mechanism for duplicate prevention.

HTTP failures use RFC Problem Details with content type
`application/problem+json`, never a successful status containing
`success: false`. The standard `type`, `title`, `status`, `detail`, and
`instance` members are extended with a stable domain `code`, safe localization
parameters, `command_id`, structured `field_errors` or typed `blockers`, and a
`retryable` flag when relevant. Application errors remain transport-neutral;
FastAPI owns the HTTP status mapping and strips stack traces, secrets, absolute
paths, and provider payloads from responses while logging them under the same
correlation ID.

Only synchronous request acceptance and validation failures use Problem
Details. Once an asynchronous command has returned `202`, later workflow
failure belongs to its Execution Attempt and reaches the client through its
resource projection and SSE events rather than retroactively becoming an HTTP
error.

Each SPA instance maintains one long-lived global SSE subscription instead of
opening streams per Project, Run, or Attempt. Durable events share one
monotonically increasing `event_id` and include a schema version, event type,
timestamp, typed owning-object reference, and relevant `command_id`,
`attempt_id`, and `job_id`. They are invalidation/progress signals; resource
`GET` projections remain the authoritative current state.

On reconnect the browser sends standard `Last-Event-ID`, and the Web adapter
replays later retained events in order. If that cursor predates retention, the
server emits `resync_required`; the client invalidates its resource cache and
refetches the visible page plus global Activity before continuing. Route changes
never close the global stream.

HTTP resources use shallow ownership routes. Listing or creating a child may be
scoped once beneath its owner, while detail queries and commands address the
target directly by its canonical ID. For example:

```text
GET  /api/projects/{project_id}/runs
POST /api/projects/{project_id}/runs
GET  /api/runs/{run_id}
POST /api/runs/{run_id}/retry
GET  /api/frozen-edits/{edit_id}
POST /api/frozen-edits/{edit_id}/revisions
POST /api/frozen-edits/{edit_id}/render-variants
```

The adapter does not mirror the full Project → Run → Frozen Edit → Variant
hierarchy in every URL. Application queries still validate owner relationships
and reject an ID that does not belong to the scoped parent; opaque IDs never let
the client infer or bypass ownership.

Browser navigation paths are separate from this HTTP route topology and may
encode a selected entity, drawer, modal, or tab for refresh and Back/Forward.
Resource-rooted page projections such as Project workspace, Run detail, and
Frozen Edit review provide coherent lightweight reads, while Material Memory,
logs, and complete candidate collections use lazy detail or paginated queries.
A route loader may issue a base page projection and selected-entity detail query
in parallel; the API does not need a single application-wide mega-response.
Project, Run, and Frozen Edit identity belong in the browser path, while Review
Variant and Slot selection may be query state because they do not change the
page hierarchy. No URL ever serializes an unsaved Revision Draft.

```text
CLI adapter ------------------\
Benchmark adapter -------------+-> CutMasterApplication
FastAPI Web adapter -----------/

app.materials -> MaterialCatalog port <- local storage implementation
app.direct / app.runs / app.renders -> resolved runtime handles
                                     -> Analyser / Planners / Renderer

Analyser -> Video Material Memory / Music Memory
Planners -> ASTERTeam -> ASTER agents -> deterministic Planners tools
Planners -> Music Memory -> Music Profile -> portable RenderPlan
Renderer -> RenderPlan + RenderRuntimeBindings -> media helpers

Renderer -X-> Analyser
Renderer -X-> Planners implementations
Renderer -X-> Prompting or model gateway
```

The Renderer must be usable with `load_renderer_config()` and no API keys.
Changing codec, resolution, BGM/dialogue mode, or audio mix may create a new
render from the same plan. Changing FPS or any edit timing requires a new plan.

For managed rendering, the Application normalizes the audio mode and every
output-affecting Renderer setting into a Render Specification key. Output
target, overwrite behaviour, Execution Attempt identity, worker concurrency,
and other execution-only controls are excluded. Within one Frozen Edit, the
Application atomically gets or creates at most one non-deleted Render Variant
for that key; a persistence uniqueness constraint prevents concurrent clicks
or requests from scheduling duplicate renders.

The normalized Render Specification is persisted immutably when the Variant is
created. It contains a render-contract version and concrete output-affecting
values rather than a config-file reference, environment lookup, secret, or
unresolved `auto` choice. Every Retry, Resume, and Render again Attempt rebuilds
its Renderer request from that snapshot and never substitutes the currently
loaded Renderer configuration. If current settings normalize differently, they
address a different Variant. The first release adds no capability preflight or
special unsupported-specification state; an unavailable historical dependency
surfaces through the ordinary Renderer Attempt failure path.

Requesting an existing Ready Variant returns it, while a Queued or Running one
returns its current progress. Failed and Interrupted Variants keep their
identity and receive Retry and Resume Attempts respectively. A new Variant is
created only when the normalized specification differs or the previous Variant
was permanently deleted.

A Ready Variant is usable only while its Managed Artifact Reference resolves to
a master that passes the recorded integrity checks. If that artifact is missing
or invalid, the Application retains the Variant and marks it `Unavailable`; no
Execution Attempt has failed, so it must not be classified as `Failed` and no
repair starts automatically. **Render again** creates a new Renderer Execution
Attempt under the same Variant and Render Specification. A successful Attempt
does not become `Ready` until the Application records the managed master's byte
size and SHA-256 alongside the Renderer-reported frame count and duration.

Application startup never scans all managed masters. Render Variant list
queries perform only bounded filesystem checks such as reference resolution,
existence, and expected byte size; an obvious mismatch is enough to persist
`Unavailable`. Before the first playback, download, Show in Finder, or reuse of
that Variant in each Application process, the Application recomputes SHA-256.
A successful result is cached in memory against Variant ID, resolved path, byte
size, and modification time; unchanged subsequent accesses skip hashing, while
any stat change invalidates the cache. This keeps launch and ordinary navigation
independent of the number and total size of rendered videos without weakening
the recorded byte-integrity check.

**Proposed Web behavior:** a Ready-to-Unavailable condition change will create
one deduplicated persistent application notification and update Run/Output
projections, but create no Activity row because it is not an Execution Attempt.
If the user requests **Render again**, only the resulting Renderer Attempt will
enter the durable job queue and Activity stream. Notification persistence,
acknowledgement, episode deduplication, and automatic resolution are not yet
implemented; they belong to the Web/managed-worker milestone.

### Benchmark compatibility boundary

Introducing the Application Layer must not require an HTTP server, SQLite
product history, or a durable worker for direct component and benchmark runs.
The following existing interfaces are compatibility contracts:

- the synchronous `cutmaster analyse`, `analyse-music`, `plan`, `render`, and
  `run` command names, arguments, exit behaviour, and JSON output for current
  raw-input flows and artifacts generated by the refactored workflow; historical v1
  RenderPlan files are explicitly outside this compatibility guarantee;
- `CutMasterApplication.open(...).direct.execute_workflow(...)`, used directly
  by the Mashup-Benchmark worker inside its existing per-task subprocess; and
- the documented three-stage artifact layout, including root `result.json`
  and `renderer/output.mp4`.

Direct calls execute without creating Project, ASTER Run, or SQLite history
records and delegate to framework-independent Application use cases. Web-managed
operations use the durable Application workflow instead.

### Complete-generation CLI contract

`cutmaster run` remains the stable one-command interface for automated and
manual evaluation. It synchronously executes the complete CutMaster Workflow
through the direct Application use case and returns only after the final video
and `result.json` have been written. It never requires FastAPI, SQLite product
history, or the durable job supervisor.

The command continues to accept either raw video/music paths or exact existing
Material Names, plus editing intent, target duration, output directory, and the
current advanced compatibility options. A successful command exits with code
zero and prints a machine-readable `WorkflowResult`; failure exits non-zero.
The separate `analyse`, `analyse-music`, `plan`, and `render` commands remain
available for component-level evaluation and debugging.

The implemented CLI surface contains those five synchronous commands plus
`cutmaster serve`. The Web slice exposes Project, Material, Activity, Settings,
and managed ASTER planning use cases. Retry/Resume, Material Analysis, Render,
Review, and other long-running Web commands wait for the proposed general
supervisor expansion.

The official Mashup-Benchmark adapter may call the same direct Application API
as a peer adapter instead of spawning this CLI adapter. Both routes execute the
same use case and must produce the same workflow result and artifact contract.

### Output ownership

**Implemented:** Direct Bundle allocation, explicit external-output validation,
Material leases, and storage reporting. **Proposed:** bulk Direct Bundle cleanup
and Data Root Migration.

Output ownership is explicit and is never inferred from the resolved path.
The Application Layer accepts three output targets:

- omitting CLI `--output-dir` allocates a unique Direct Workflow Bundle at
  `direct/bundle_<uuid>/` beneath the Application Data Root;
- an explicit CLI `--output-dir` is an external output and must resolve outside
  the Application Data Root; and
- a managed product use case supplies an owning entity ID plus a root-relative
  Managed Artifact Reference. CLI callers cannot construct this target by
  passing a path.

Direct Workflow Bundles count toward Application Data Root storage usage but do
not create Project, ASTER Run, Activity, or Render Variant records. External
outputs are not managed by CutMaster. Paths inside `media/`, `projects/`,
`direct/`, or any other Data Root namespace are rejected when supplied as
explicit CLI output directories. The proposed Data Root Migration will move
managed Direct Bundles together with the rest of the root.

Direct Workflow Bundles have no automatic retention deadline. Settings reports
their count and total size. An explicit bulk-delete operation with two-click
confirmation is **Proposed**; it will skip active or locked bundles and report
both skipped and deleted counts.

A direct workflow holds Material leases for its lifetime, so Material deletion
cannot race CLI or Benchmark use. The proposed Data Root Migration must add an
exclusive root lease before copying or switching managed state.

### Implemented Benchmark adapter

Mashup-Benchmark is a peer inbound adapter, not a client of the CLI or FastAPI
Web adapters. Its worker keeps the existing per-task
subprocess boundary but calls the synchronous direct-execution API exposed by
the Application Layer. CLI and Benchmark both call `app.direct`; the former
complete-workflow facade and `WorkflowRequest` contract have therefore been
removed.

The direct workflow result publishes a versioned logical `artifacts` mapping
inside `result.json`. Benchmark integrations consume those returned references
instead of reconstructing paths such as `planners/script_raw.json`. Existing
top-level result fields and the documented artifact layout remain available for
compatibility. Manifest version `1.0` uses stable dotted logical keys and
normalized POSIX paths relative to the directory containing `result.json`.
Benchmark rejects an unsupported major version, ignores unknown keys from a
compatible minor version, and treats a missing required key as an invalid
successful result.

Benchmark media records may provide an explicit stable `material_name`. When
that field is absent, the adapter uses the existing local-path filename stem,
preserving current Material identities. The adapter passes the resolved video
and music names in `ExecuteWorkflowCommand`, which makes reuse independent of
the per-task output directory and turns changed bytes under a known name into
an explicit consistency error.

## Direct compatibility artifact layout

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
├── model_usage.json
└── cutmaster.log
```

The run-local Analyser results identify the selected Materials; their reusable
memory lives in the active Material Catalog beneath the selected Application
Data Root. The root files summarize the whole workflow. Every other artifact is
owned by exactly one stage.

On successful complete execution, `result.json` preserves its existing top-level
compatibility fields and adds:

```json
{
  "artifact_manifest_version": "1.0",
  "artifacts": {
    "analyser.video_result": "analyser/analysis_result.json",
    "planners.render_plan": "planners/render_plan.json",
    "renderer.output_video": "renderer/output.mp4"
  }
}
```

The mapping contains file paths only. Paths are normalized POSIX paths relative
to the bundle root and cannot be absolute, contain backslashes or `.`/`..`
segments, or resolve through a symlink outside that root. Optional artifacts are
omitted rather than represented by `null`; stale files from an overwritten run
are never published. `application/direct/artifacts.py` owns the logical-key
registry and validation, and atomically publishes `result.json` only after every
required non-self artifact exists. The full v1 key registry and compatibility
rules are fixed by
[ADR 0020](adr/0020-publish-a-versioned-direct-artifact-manifest.md).

## Public APIs

The implemented composition API for every inbound adapter is:

```python
from pathlib import Path

from cutmaster import CutMasterApplication

app = CutMasterApplication.open(Path("config.toml"))

app.direct       # synchronous component and complete-workflow execution
app.materials    # Material lifecycle and memory queries
app.projects     # Edit Project and Creative Brief
app.runs         # ASTER Runs and Guided Revision
app.renders      # Render Variants
app.jobs         # Attempts, Activity, Stop, Retry, Resume, durable events
app.settings     # models, execution settings, storage, and Data Root
```

`CutMasterApplication.open(...)`, all seven lazy service groups, Direct and
Material operations, SQLite-backed managed use cases, and Settings reads,
writes, and storage reporting are implemented. CLI and Mashup-Benchmark both
invoke the Direct service; FastAPI maps its supported routes to the same
Application and dispatches implemented ASTER planning in a managed subprocess.
Persistent application notifications, SSE, the general supervisor and other
managed workers, and Data Root Migration are **Proposed** additions over this
boundary.

The complete direct workflow uses a stable versioned contract from
`cutmaster.contracts.workflow`:

```python
from cutmaster.contracts import ExecuteWorkflowCommand, WorkflowResult

def execute(command: ExecuteWorkflowCommand) -> WorkflowResult:
    return app.direct.execute_workflow(command)
```

Command construction is omitted only to keep this architecture example
independent of its field list. The direct command and `WorkflowResult` remain
usable by peer adapters without importing `application/` implementation
modules. A successful `WorkflowResult` exposes
`artifact_manifest_version == "1.0"` and `artifacts: dict[str, str]`; consumers
resolve those references against the Workflow Bundle root associated with the
command—for a serialized result, the parent of `result.json`—rather than
treating the values as process-relative or absolute paths.

| Surface | Stability |
|---|---|
| `CutMasterApplication.open(...)` and its seven grouped services | Permanent public composition API |
| `ExecuteWorkflowCommand` and versioned `WorkflowResult` | Permanent public direct-workflow contract |
| Root `Analyser`, `Planners`, and `Renderer` class names/imports | Permanent public stage surface |
| Stage request DTO signatures | v2 handle-only contracts; former raw-path constructors are not retained |
| Removed complete-workflow facade, `WorkflowRequest`, and `Analyser.resolve_*` | Use `app.direct` / `app.materials` |
| MASTER agents, tools, cache paths, fingerprints, repositories, and absolute managed paths | Internal, never public |

The v2 stage boundary is deliberately not dual-mode. Analyser receives resolved
Material handles; Planners receives analysed video/music handles plus planning
brief and options; Renderer receives a portable RenderPlan, explicit
`RenderRuntimeBindings`, RenderOptions, and a structured output target. Callers
cannot fabricate supported handles from arbitrary paths. CLI command and
Benchmark compatibility is provided by `app.direct`, which resolves inputs and
constructs the v2 stage requests. See
[ADR 0019](adr/0019-use-handle-only-v2-stage-contracts.md).
