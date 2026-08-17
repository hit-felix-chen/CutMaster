"""Execute one durable managed Renderer Job inside the Application boundary."""

from __future__ import annotations

import os
from collections.abc import Mapping
from threading import Event, Thread

from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import (
    ClaimJobCommand,
    FailAttemptCommand,
    HeartbeatJobCommand,
    JobSubmissionView,
)
from cutmaster.application.jobs.lease import validate_claimed_submission
from cutmaster.application.renders.execution import (
    ExecuteManagedRenderCommand,
    RenderExecutionInterrupted,
)
from cutmaster.domain.attempts import TERMINAL_ATTEMPT_STATUSES, AttemptStatus
from cutmaster.domain.ids import AttemptId, JobId, RenderVariantId
from cutmaster.infrastructure.observability.logging import error_summary


def execute_render_job(
    application: CutMasterApplication,
    job_id: JobId,
    *,
    renderer_factory=None,
    cover_generator=None,
    worker_id: str | None = None,
    process_id: int | None = None,
    heartbeat_interval_sec: float = 10.0,
    claimed_submission: JobSubmissionView | None = None,
) -> AttemptStatus | None:
    """Claim one exact Renderer job and execute its immutable snapshot."""

    if not isinstance(application, CutMasterApplication):
        raise TypeError("application must be a CutMasterApplication")
    if not isinstance(job_id, JobId):
        raise TypeError("job_id must be a JobId")
    if heartbeat_interval_sec <= 0:
        raise ValueError("heartbeat_interval_sec must be positive")
    resolved_process_id = os.getpid() if process_id is None else process_id
    if claimed_submission is None:
        claimed = application.jobs.claim_next(
            ClaimJobCommand(
                worker_id or f"web-render-{resolved_process_id}",
                resolved_process_id,
                job_id,
            )
        )
    else:
        validate_claimed_submission(claimed_submission, job_id)
        claimed = claimed_submission
    if claimed is None:
        return None
    attempt = claimed.attempt
    if attempt.operation_type != "rendering" or attempt.owner_type != "render_variant":
        application.jobs.mark_failed(
            FailAttemptCommand(
                attempt.attempt_id,
                "Managed Renderer executor received a non-rendering Job",
            )
        )
        return AttemptStatus.FAILED

    stop_heartbeat = Event()
    heartbeat = Thread(
        target=_heartbeat_loop,
        args=(application, job_id, stop_heartbeat, heartbeat_interval_sec),
        name=f"cutmaster-render-heartbeat-{job_id}",
        daemon=True,
    )
    heartbeat.start()

    def should_stop() -> bool:
        return application.jobs.get_job(job_id).stop_requested

    def report_progress(progress: Mapping[str, object]) -> None:
        application.jobs.heartbeat(HeartbeatJobCommand(job_id, progress))

    try:
        keyword = {
            "should_stop": should_stop,
            "report_progress": report_progress,
        }
        if renderer_factory is not None:
            keyword["renderer_factory"] = renderer_factory
        if cover_generator is not None:
            keyword["cover_generator"] = cover_generator
        completed = application.renders.execute_attempt(
            ExecuteManagedRenderCommand(
                render_variant_id=RenderVariantId.parse(attempt.owner_id),
                attempt_id=attempt.attempt_id,
            ),
            **keyword,
        )
        return completed.attempt.status
    except RenderExecutionInterrupted:
        latest = application.jobs.get_attempt(attempt.attempt_id)
        if latest.status not in TERMINAL_ATTEMPT_STATUSES:
            application.jobs.mark_interrupted(attempt.attempt_id)
        return AttemptStatus.INTERRUPTED
    except Exception as error:  # noqa: BLE001 - worker failure boundary
        _finish_failed_or_interrupted(
            application,
            job_id,
            attempt.attempt_id,
            error,
        )
        return application.jobs.get_attempt(attempt.attempt_id).status
    finally:
        stop_heartbeat.set()
        heartbeat.join(timeout=min(heartbeat_interval_sec, 1.0))


def _heartbeat_loop(
    application: CutMasterApplication,
    job_id: JobId,
    stop: Event,
    interval_sec: float,
) -> None:
    while not stop.wait(interval_sec):
        try:
            application.jobs.heartbeat(HeartbeatJobCommand(job_id))
        except Exception:  # noqa: BLE001 - the worker owns terminal persistence
            return


def _finish_failed_or_interrupted(
    application: CutMasterApplication,
    job_id: JobId,
    attempt_id: AttemptId,
    error: Exception,
) -> None:
    attempt = application.jobs.get_attempt(attempt_id)
    if attempt.status in TERMINAL_ATTEMPT_STATUSES:
        return
    job = application.jobs.get_job(job_id)
    if job.stop_requested:
        application.jobs.mark_interrupted(attempt_id)
    else:
        message = error_summary(error) or type(error).__name__
        application.jobs.mark_failed(FailAttemptCommand(attempt_id, message))


__all__ = ["execute_render_job"]
