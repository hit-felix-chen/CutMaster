# Use one migratable Application Data Root

**Status: implemented, including the guarded background migration and Settings
control plane.**

The backend stores all managed local state beneath one Application Data Root,
defaulting to `CutMaster/.cutmaster/`, while allowing the user to choose another
local directory. The path changes through a guarded Data Root Migration that requires no active
Attempt, pauses queued-job dispatch, copies and verifies the complete canonical
managed database and filesystem state, and atomically switches only after
success. Historical and external content such as
`.cutmaster/materials-backup/` and pre-refactor workflow-output directories is
excluded and left untouched. Keeping
one root avoids split-brain project and Material state; preserving the original
as authoritative until verification prevents a failed move from making
existing work unavailable.

Migration is admitted only when no Execution Attempt is Running, Retrying, or
Stopping, and it never stops active work implicitly. A stable external control
database and `flock` lease pause new dispatch and fence every persistent
Application read or mutation across Web, CLI, Benchmark, and worker processes.
The background worker creates an online SQLite backup, copies the explicit
canonical namespaces, verifies sizes, SHA-256 digests, SQLite integrity,
foreign keys, schema, and queued-job count, then atomically replaces the
external pointer. During maintenance every business API read and write returns
a typed 503; only health, migration control/status, and static SPA resources
remain available. A successful switch enters `restart_required`; the next
backend start validates the destination owner marker and completes the durable
migration. Cancellation or failure removes only manifest-owned staging files,
keeps unknown external entries, leaves the original pointer authoritative, and
restores dispatch.

All current workflow artifacts belong to managed Materials, Projects, Runs,
Frozen Edits, Render Variants, Attempts, or logs and move with the root.
