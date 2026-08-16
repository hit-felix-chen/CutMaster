# Run long work in durable local subprocess jobs

**Status: SQLite queue, Attempt state, heartbeats, ordered event queries, and
the Web ASTER planning subprocess implemented; general supervisor and SSE
proposed.**

The Application Layer writes managed long-running CutMaster commands to a
SQLite-backed queue. The implemented Web ASTER dispatcher launches an isolated
subprocess for each submitted planning job; that worker claims the exact job,
heartbeats, persists the real A/S/T/E/R agent boundary as its latest structured
progress snapshot, calls the real Planners use case, atomically publishes its
RenderPlan and versioned Review Candidate Bundle, and completes the Run with an
initial Frozen Edit. Ordinary heartbeats update liveness without overwriting
that progress snapshot. FastAPI only adapts and accepts the HTTP command. The
current SPA polls one lightweight
SQLite execution projection for Runs, Run detail, and Activity while an Attempt
is active, so those views derive their displayed state from the same Attempt and
Job. The general supervisor that extends this boundary to Material Analysis,
Renderer, automatic Preview/Variant creation, orphan recovery, and shared queue
scheduling remains proposed. Frozen Edit Review and atomic Guided Revision are
synchronous Application operations outside that supervisor. Redis and
distributed task systems are deliberately deferred for the local single-user
release.

The existing SPA/FastAPI adapter will eventually use one global SSE stream
backed by the implemented monotonic durable event log. `Last-Event-ID` replay,
`resync_required`, and REST projection recovery belong to that Proposed adapter;
the current backend exposes ordered event queries but no SSE transport.
