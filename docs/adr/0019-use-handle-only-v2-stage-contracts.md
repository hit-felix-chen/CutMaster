# Use handle-only v2 contracts for Workflow stages

**Status: Implemented.**

CutMaster keeps the public `Analyser`, `Planners`, and `Renderer` class names and
their root-package import paths, and their v2 request contracts are
handle-only. The former Python request signatures that accept raw video, music,
subtitle, plan, or output paths are not retained as overloads, union types, or
compatibility wrappers at the Workflow boundary.

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

CLI compatibility is preserved above this breaking boundary. The existing
`analyse`, `analyse-music`, `plan`, `render`, and `run` commands continue to
accept their documented file/name-facing inputs and invoke `app.direct`, which
constructs v2 stage requests. Mashup-Benchmark likewise calls
`app.direct.execute_workflow(ExecuteWorkflowCommand)` and preserves its worker
subprocess and artifact contract. Both callers have migrated; the transitional
complete-workflow facade and `WorkflowRequest` are removed.

The completed migration followed this order:

1. Introduce `CutMasterApplication`, Material resolution, and the synchronous
   direct component/workflow use cases.
2. Migrate CLI and Mashup-Benchmark to those use cases and pass their
   compatibility tests.
3. Replace stage request schemas and implementations with handle-only v2
   contracts and remove the raw-path stage DTO definitions, keeping only the
   three stage class names/import paths stable.
4. Remove the former complete-workflow facade, `WorkflowRequest`, and
   `Analyser.resolve_*` selectors.

This avoids two authorities for Material lifecycle, makes leases and
fingerprint validation unavoidable, and prevents a supposedly portable
Workflow contract from depending on caller-selected filesystem paths. The cost
is an intentional breaking change for direct Python callers that instantiate
the old stage request DTOs; those callers must move to `app.direct` or construct
v2 requests from Application-issued handles.
