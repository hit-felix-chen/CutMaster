# Treat CLI, Web, and Benchmark as peer adapters

**Status: CLI, FastAPI Web, and Mashup-Benchmark peer adapters implemented.**

CutMaster uses a framework-independent Application Layer as the shared use-case
boundary for peer inbound adapters. CLI, FastAPI Web, and Mashup-Benchmark are
implemented. The Benchmark worker calls the synchronous direct-execution
Python API rather than invoking CLI or HTTP, while Web-managed work uses the
durable Application workflow. This keeps typed integration and per-task process
isolation without creating an adapter-to-adapter dependency.

All three adapters obtain grouped use-case services from one
`CutMasterApplication` composition entry point. It exposes `direct`,
`materials`, `projects`, `runs`, `renders`, `jobs`, and `settings` services and
contains no business workflow implementation itself, preventing the shared
facade from becoming a monolithic application service. The `settings` group
owns effective model/execution settings, atomic non-secret overlay writes, and
storage reporting. Data Root Migration and its Settings flow are Proposed;
browser-only Locale and Colour Mode preferences remain outside Application
state.

The existing CLI commands and three-stage artifact layout remain compatibility
contracts. CLI and Benchmark have migrated to the direct Application use case;
the former complete-workflow facade and `WorkflowRequest` were removed rather
than retained as another architecture layer. The stable
`contracts/workflow.py` module remains and exposes
`ExecuteWorkflowCommand` plus the versioned direct result. Direct results expose
a v1 logical Artifact Manifest containing bundle-root-relative POSIX paths, and
the Benchmark adapter consumes its stable keys rather than inferring internal
paths. It passes a stable Material Name so it can reuse Material Memory without
depending on inferred paths or fingerprints. The synchronous `cutmaster run`
command remains a stable one-command complete-video interface for evaluation;
it invokes the same direct Application use case without requiring Web or
managed application state. The implemented CLI exposes the five synchronous
stage/workflow commands plus `serve`. Web exposes Project, Material, Activity,
Settings, and managed ASTER planning use cases; Start editing dispatches a real
Planners subprocess. Material Analysis, Renderer, Review, and SSE wait for the
proposed general supervisor expansion. All adapters
open one Application Data
Root and one Material Catalog. Missing CLI output paths allocate Direct Workflow
Bundles under that root, explicit paths remain external, and only managed use
cases can allocate entity-owned artifacts.

The manifest schema, required keys, and safe relative-path rules are defined by
[ADR 0020](0020-publish-a-versioned-direct-artifact-manifest.md).
