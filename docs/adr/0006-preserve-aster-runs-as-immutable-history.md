# Preserve ASTER Runs as immutable history

**Status: SQLite history, Application operations, Web Start editing, and the
managed ASTER planning worker implemented.**

The project layer models every Start editing command as a new immutable ASTER
Run that snapshots its Material References, Creative Brief, and effective
non-secret configuration. Its Execution Attempts represent each try; successful
Planners completion records a RenderPlan reference that the Application
atomically commits as the initial Frozen Edit. Frozen Edit owns that plan as its
sole timeline payload rather than duplicating its contents in database columns.
It supplies the stable edit-version identity used by Review, Guided Revision
lineage, and multiple Render Variants; unmanaged direct plans do not acquire
this product identity. Changing any ASTER input creates another Run instead of
overwriting an earlier one, preserving reproducibility and comparison at the
cost of retaining additional project history. Direct Workflow usage artifacts
are implemented; Run-level usage projection remains proposed.

New managed completions also publish a versioned Candidate Bundle as immutable
Review support data. Its absence on a historical Frozen Edit does not invalidate
the Frozen Edit or RenderPlan: Review remains available in read-only form, while
Guided Revision is enabled only when the real bundle passes integrity checks.
Saving a valid Guided Revision atomically creates a child Frozen Edit and never
overwrites its source; automatic Renderer preview creation remains proposed.

Direct execution writes one set of Planners artifacts to its Direct Bundle or
external output directory and does not create project history. The implemented
Web command assigns each ASTER Run its independent
`projects/<project-id>/runs/<run-id>/plan.json` artifact and dispatches an
isolated subprocess instead of invoking it with `overwrite=true`. A failed Run
may have multiple Execution Attempts as long as
its input snapshot remains unchanged; a retry is recovery history, not a new
ASTER Run. Retry and Resume preserve that same non-secret snapshot even if
global Settings have changed. The ASTER worker resolves API keys when its
Attempt starts so repaired credentials can take effect without altering the
Run.
