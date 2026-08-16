# Protect referenced Materials from deletion

**Status: Backend reference checks implemented; Web confirmation flow proposed.**

The frontend may delete a Material only after the user removes every current
project selection and explicitly deletes every ASTER Run that references it.
There is no force-delete path: deleting a Run cascades to its Frozen Edits and
Render Variants, Attempts, jobs, Activity records, and other SQLite-owned state
but preserves the Edit Project. Deleting corresponding managed logs and
artifacts is part of the proposed worker/artifact-store integration and is not
performed by the current Run deletion service. This keeps historical plans
reproducible and rerenderable without forcing users to delete an entire project
just to release one Material.

No Material, Run, or Project may be deleted while it owns an Execution Attempt
that is Queued, Running, Retrying, or Stopping. The user must Stop the work and
wait for Interrupted first; deletion never implicitly cancels work or kills a
subprocess that may still write owned artifacts.

Frozen Edits cannot be deleted independently because they preserve revision
lineage and the shared editorial decision behind multiple outputs. Render
Variants may be permanently deleted one at a time after all their Attempts are
terminal, removing their managed media and owned records without changing the
Frozen Edit. Deletion schedules no replacement render; Review stays on the
unchanged edit until the user explicitly requests a new Variant, which receives
a new identity.
