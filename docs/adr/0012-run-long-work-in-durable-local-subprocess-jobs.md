# Run long work in durable local subprocess jobs

**Status: Implemented for Material Analysis, ASTER planning, Renderer, shared
scheduling, cancellation, orphan recovery, and durable SSE.**

The Application Layer writes managed long-running CutMaster commands to a
SQLite-backed queue. One `LocalJobSupervisor` claims work in durable FIFO order
subject to configured capacity and per-owner serialization, then launches an
isolated Material Analysis, ASTER planning, or Renderer subprocess. Workers
claim the exact job, heartbeat without overwriting structured progress, call
the real stage use case, and atomically publish owned artifacts and state.
FastAPI only adapts and accepts HTTP commands.

Analyser and Planners cooperatively check the shared stop signal at safe model,
media, and agent boundaries; Renderer and worker orchestration use the same
Attempt stop state. A requested stop reaches `Interrupted`, never `Failed`.
Supervisor recovery uses durable process/lease information to distinguish live
workers from orphans, restore abandoned work safely, and avoid duplicate
launches after concurrent server starts or worker exit.

ASTER Resume pins a validated identity-bound checkpoint from an Interrupted
Attempt. Checkpoints cover only completed A/S/T/E/R agent boundaries plus the
internal replan-pending boundary; they contain parsed workflow state and
aggregate usage, not prompts, credentials, provider payloads, raw paths, or
instruction-level continuation state.

The SPA keeps one global SSE connection backed by the monotonic durable event
log. Reconnect uses `Last-Event-ID` to replay retained later events in order. If
the cursor predates retention, the server emits `resync_required` and the client
invalidates its cache and refetches authoritative REST projections. Polling
remains a fallback when SSE is unavailable. Redis and distributed task systems
remain deliberately deferred for the local single-user release.
