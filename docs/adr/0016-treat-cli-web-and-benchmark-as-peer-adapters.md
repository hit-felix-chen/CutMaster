# Treat CLI, Web, and Benchmark as peer adapters

**Status: CLI, FastAPI Web, and Mashup-Benchmark peer adapters implemented;
complete CLI and Benchmark runs use managed history.**

CutMaster uses a framework-independent Application Layer as the shared use-case
boundary for peer inbound adapters. CLI, FastAPI Web, and Mashup-Benchmark are
implemented. The Benchmark worker calls the typed managed local API rather than
invoking CLI or HTTP. CLI and Benchmark synchronously drive the same durable
Job executors that Web schedules through its supervisor. This keeps typed
integration and per-task process isolation without creating an HTTP dependency.

All three adapters obtain grouped use-case services from one
`CutMasterApplication` composition entry point. It exposes `direct`,
`materials`, `projects`, `runs`, `renders`, `jobs`, and `settings` services and
contains no business workflow implementation itself, preventing the shared
facade from becoming a monolithic application service. The `settings` group
owns effective model/execution settings, atomic `config.toml` writes, and
storage reporting. Data Root Migration and its Settings flow are implemented;
browser-only Locale and Colour Mode preferences remain outside Application
state.

CLI and Benchmark have migrated from caller-owned Direct Workflow Bundles to
the managed Material, Project, Run, Frozen Edit, and Render Variant use cases;
the former complete-workflow facade and `WorkflowRequest` remain removed. The
stable managed contract exposes `ExecuteManagedWorkflowCommand` plus a
versioned receipt containing Application-Data-Root-relative artifact paths.
The Benchmark adapter validates those keys against that root and copies the
evaluation artifacts into its own Run directory. It passes stable Material
Names so analysis can be reused without depending on paths or fingerprints.
The synchronous `cutmaster run` command remains a one-command complete-video
interface and creates history that can be opened in Web. The implemented CLI exposes the five synchronous
stage/workflow commands plus `serve`. Web exposes Project, Material, Activity,
Settings/Setup, the complete managed Material Analysis and ASTER Run lifecycles,
Frozen Edit Review, atomic Guided Revision, Renderer Preview/Variant/Outputs,
and guarded Data Root Migration. The shared local supervisor dispatches real
Analyser, Planners, and Renderer subprocesses, and durable SSE projects their
state to the client. All adapters open one Application Data
Root and one Material Catalog. CLI exposes no custom output directory; canonical
artifacts are entity-owned beneath that root. Benchmark copies are explicitly
secondary submission artifacts.

The managed-history decision and export boundary are defined by
[ADR 0021](0021-store-cli-and-benchmark-runs-as-managed-history.md). ADR 0020
continues to define the older Direct Bundle contract for remaining direct API
callers.
