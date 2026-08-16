# Use one migratable Application Data Root

**Status: root resolution and managed namespaces implemented; guarded migration
and its Settings UI proposed.**

The backend stores all managed local state beneath one Application Data Root,
defaulting to `CutMaster/.cutmaster/`, while allowing the user to choose another
local directory. An empty root may be changed directly, but once data exists the
path changes only through a guarded Data Root Migration that requires no active
Attempt, pauses queued-job dispatch, copies and verifies the complete canonical
managed database and filesystem state, and atomically switches only after
success. Direct Workflow Bundles stored under `direct/` are application-owned
and will move with the root even though they are not project history. Historical and
external content such as `.cutmaster/materials-backup/` is excluded. Keeping
one root avoids split-brain project and Material state; preserving the original
as authoritative until verification prevents a failed move from making
existing work unavailable.

The proposed migration will be available only when no Execution Attempt is
Running, Retrying, or Stopping, and it will never stop active work implicitly.
Its supervisor/root-lease implementation will pause dispatch and managed-state
mutation during copy and verification so migration cannot race CLI, Benchmark,
or queued-job writes. None of that migration coordination is implemented yet.

Direct Workflow Bundles have no automatic retention policy. Settings reports
their count and size. Bulk deletion with two-click confirmation and active-lock
skipping is Proposed.
