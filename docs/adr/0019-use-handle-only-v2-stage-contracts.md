# Use handle-only v2 contracts for Workflow stages

**Status: Implemented.**

CutMaster keeps the public `Analyser`, `Planners`, and `Renderer` class names and
their root-package import paths, and their v2 request contracts are handle-only.
Python request signatures that accept raw video, music, subtitle, plan, or
output paths are not retained as overloads, union types, or alternate Workflow
boundaries.

The Application Layer owns every transition from caller-facing input to a
Workflow request. It resolves a raw path or exact Material Name through the
Material Catalog, verifies the bound fingerprint, acquires the required lease,
and constructs the runtime handle and stage workspace. Consequently:

- Analyser receives a resolved Material Runtime Handle plus analysis options and
  an allocated stage workspace. A video's optional imported subtitle is an
  immutable Material-owned sidecar whose verified runtime reference travels in
  that handle. Analyser builds or reuses Material Memory but never imports,
  names, ensures, resolves, replaces, or deletes a Material.
- Planners receives analysed video and music handles plus the Planners Brief,
  Planners Options, and an allocated Planners Workspace. It never resolves public
  names or source paths.
- Renderer receives a portable v2 RenderPlan, explicit Render Runtime Bindings,
  Render Options, and a structured output target. It never resolves Materials
  or accepts a v1 absolute-path plan.

Runtime handles are valid only when created by the Application composition and
used under their owning lease. Manually fabricating one from an arbitrary path
is outside the public contract. Contract construction and validation perform no
filesystem or media I/O. Stage implementations currently use the concrete local
model, media, logging, and progress helpers under `infrastructure/`; expanding
those helpers behind provider/media Workflow ports is a separate Proposed
refactor and is not part of the completed handle-only request migration.

CLI, the FastAPI Web adapter, the Worker process adapter, and Mashup-Benchmark
are peers above this boundary. They call `CutMasterApplication.workflows` or
another grouped Application service; none calls another adapter. The existing
`analyse`, `analyse-music`, `plan`, `render`, and `run` commands accept only
their documented managed inputs. Mashup-Benchmark imports
`ExecuteManagedWorkflowCommand` from `cutmaster.application.workflow`, calls
`app.workflows.execute_and_wait(command)`, and copies only the managed artifacts
required for evaluation.

`ManagedWorkflowCoordinator` owns the complete Material -> Project -> ASTER Run
-> Frozen Edit -> Render Variant lifecycle. Application-owned Material, Run,
and Render executors construct the handle-only stage requests, while the durable
Job executor is the one execution surface used by synchronous coordination and
the peer Worker process adapter.

The completed migration followed this order:

1. Introduce `CutMasterApplication`, Material resolution, and the managed
   Project/Run/Render lifecycle.
2. Make CLI, Web, Worker, and Mashup-Benchmark peer adapters over
   `CutMasterApplication` and the public `application.workflow` contract.
3. Replace stage request schemas and implementations with handle-only v2
   contracts and remove the raw-path stage DTO definitions, keeping only the
   three stage class names/import paths stable.
4. Remove alternate raw-path workflow entry points and selectors.

This avoids two authorities for Material lifecycle, makes leases and
fingerprint validation unavoidable, and prevents a portable Workflow contract
from depending on caller-selected filesystem paths. Python callers that need a
complete workflow use `app.workflows`; advanced stage callers must obtain v2
runtime handles from Application-owned execution services rather than fabricate
them from paths.
