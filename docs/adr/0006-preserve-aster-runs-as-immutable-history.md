# Preserve ASTER Runs as immutable history

**Status: SQLite history and the complete managed Web ASTER Run lifecycle
implemented.**

The project layer models every Start editing command as a new immutable ASTER
Run that snapshots its Material References, Creative Brief, and effective
non-secret configuration. Its Execution Attempts represent each try; successful
Planners completion records a RenderPlan reference that the Application
atomically commits as the initial Frozen Edit. Frozen Edit owns that plan as its
sole timeline payload rather than duplicating its contents in database columns.
It supplies the stable edit-version identity used by Review, Guided Revision
lineage, and multiple Render Variants. Changing any ASTER input creates another Run instead of
overwriting an earlier one, preserving reproducibility and comparison at the
cost of retaining additional project history. Run/Attempt usage projections
are implemented for every inbound adapter.

Every managed Frozen Edit requires a versioned Candidate Bundle as immutable
Review support data. The bundle must be present, pass integrity checks, and map
every required Slot to its selected candidate. Missing, damaged, or incomplete
Review data invalidates the Frozen Edit's Review projection with
`review_artifact_unavailable`; there is no RenderPlan-only or read-only fallback.
Saving a valid Guided Revision atomically creates a child Frozen Edit and never
overwrites its source. Managed Renderer Variants and the default Dialogue
Preview are implemented against explicit Frozen Edit identities.

CLI, Web, and Benchmark execution all create the same managed history. The
Application assigns each ASTER Run its independent
`projects/<project-id>/runs/<run-id>/plan.json` artifact and dispatches an
isolated worker instead of invoking it with `overwrite=true`. A failed Run
may have multiple Execution Attempts as long as
its input snapshot remains unchanged; a retry is recovery history, not a new
ASTER Run. Retry and Resume preserve that same non-secret snapshot even if
global Settings have changed. The ASTER worker resolves API keys when its
Attempt starts so repaired credentials can take effect without altering the
Run.

Resume is stricter than Retry: only an Interrupted Attempt with a valid,
identity-bound checkpoint may resume. Checkpoints are written after complete
A/S/T/E/R agent boundaries; an internal replan-pending boundary preserves
sanitized feedback before the next Arrangement Architect pass. Resume never
continues inside an agent instruction, provider call, or media operation. Run
again creates a new immutable Run from the historical snapshot, while guarded
Run deletion removes only that Run's owned lineage, attempts, outputs, events,
logs, and artifacts.
