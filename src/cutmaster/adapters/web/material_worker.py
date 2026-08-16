"""Execute one durable Material Analysis Job outside the Web process."""

from __future__ import annotations

import argparse
import os
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Event, Thread

from cutmaster.adapters.web.job_cancellation import DatabaseJobCancellationToken
from cutmaster.adapters.web.worker_lease import (
    add_supervisor_lease_arguments,
    adopt_supervisor_lease,
    validate_claimed_submission,
)
from cutmaster.application import CutMasterApplication
from cutmaster.application.direct import AnalyseMusicCommand, AnalyseVideoCommand
from cutmaster.application.jobs import (
    ClaimJobCommand,
    FailAttemptCommand,
    HeartbeatJobCommand,
    JobSubmissionView,
)
from cutmaster.application.materials import MaterialView
from cutmaster.domain.attempts import TERMINAL_ATTEMPT_STATUSES, AttemptStatus
from cutmaster.domain.ids import AttemptId, JobId, MaterialId
from cutmaster.domain.materials import MaterialCondition, MaterialType
from cutmaster.infrastructure.storage.local.material_catalog import (
    MaterialInconsistentError,
)
from cutmaster.infrastructure.observability.logging import error_summary
from cutmaster.workflow.ports import (
    CancellationToken,
    WorkflowCancelledError,
)

MaterialAnalyser = Callable[[CutMasterApplication, MaterialView, Path], None]


def execute_material_job(
    application: CutMasterApplication,
    job_id: JobId,
    *,
    analyser: MaterialAnalyser | None = None,
    worker_id: str | None = None,
    process_id: int | None = None,
    heartbeat_interval_sec: float = 10.0,
    claimed_submission: JobSubmissionView | None = None,
) -> AttemptStatus | None:
    """Claim and execute exactly one queued Material Analysis Job."""

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
                worker_id or f"web-material-{resolved_process_id}",
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
    if (
        attempt.operation_type != "material_analysis"
        or attempt.owner_type != "material"
    ):
        application.jobs.mark_failed(
            FailAttemptCommand(
                attempt.attempt_id,
                "Material worker received a non-Material Analysis Job",
            )
        )
        return AttemptStatus.FAILED
    cancellation_token = DatabaseJobCancellationToken(application.jobs, job_id)

    material_id = MaterialId.parse(attempt.owner_id)
    material = application.materials.get(material_id)
    if material is None:
        application.jobs.mark_failed(
            FailAttemptCommand(
                attempt.attempt_id,
                f"Material {material_id} is unavailable",
            )
        )
        return AttemptStatus.FAILED

    stop_heartbeat = Event()
    heartbeat = Thread(
        target=_heartbeat_loop,
        args=(application, job_id, stop_heartbeat, heartbeat_interval_sec),
        name=f"cutmaster-material-heartbeat-{job_id}",
        daemon=True,
    )
    heartbeat.start()
    try:
        _progress(application, job_id, material, "preparing", completed=0)
        cancellation_token.raise_if_cancelled()
        with tempfile.TemporaryDirectory(prefix=f"cutmaster-{material_id}-") as raw:
            workspace = Path(raw).resolve()
            _progress(application, job_id, material, "analysing", completed=1)
            if analyser is None:
                _analyse_material(
                    application,
                    material,
                    workspace,
                    cancellation_token=cancellation_token,
                )
            else:
                cancellation_token.raise_if_cancelled()
                analyser(application, material, workspace)
                current = application.materials.get(material.material_id)
                if current is None or current.condition is not MaterialCondition.READY:
                    cancellation_token.raise_if_cancelled()
        _progress(application, job_id, material, "publishing", completed=2)
        completed = application.jobs.complete_material_analysis(attempt.attempt_id)
        return completed.attempt.status
    except WorkflowCancelledError:
        attempt_view = application.jobs.get_attempt(attempt.attempt_id)
        if attempt_view.status not in TERMINAL_ATTEMPT_STATUSES:
            application.jobs.mark_interrupted(attempt.attempt_id)
        return application.jobs.get_attempt(attempt.attempt_id).status
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


def _analyse_material(
    application: CutMasterApplication,
    material: MaterialView,
    workspace: Path,
    *,
    cancellation_token: CancellationToken | None = None,
) -> None:
    # Resolve private managed paths only for the synchronous Direct boundary.
    # Direct re-validates the exact name/fingerprint binding before analysis.
    with application.materials.lease(material.material_id) as binding:
        source_path = binding.source_path
        subtitle_path = (
            application.materials.resolve_subtitle(binding)
            if material.material_type is MaterialType.VIDEO
            else None
        )

    if material.material_type is MaterialType.VIDEO:
        application.direct.analyse_video(
            AnalyseVideoCommand(
                video_path=source_path,
                output_dir=workspace,
                video_title=material.name,
                subtitle_path=subtitle_path,
                material_name=material.name,
                cancellation_token=cancellation_token,
            )
        )
    else:
        application.direct.analyse_music(
            AnalyseMusicCommand(
                audio_path=source_path,
                output_dir=workspace,
                material_name=material.name,
                cancellation_token=cancellation_token,
            )
        )


def _progress(
    application: CutMasterApplication,
    job_id: JobId,
    material: MaterialView,
    state: str,
    *,
    completed: int,
) -> None:
    application.jobs.heartbeat(
        HeartbeatJobCommand(
            job_id,
            {
                "schema_version": "1.0",
                "phase": "analyser",
                "material_type": material.material_type.value,
                "state": state,
                "completed": completed,
                "total": 3,
                "unit": "stage",
            },
        )
    )


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
        return
    if isinstance(error, MaterialInconsistentError):
        try:
            application.jobs.heartbeat(
                HeartbeatJobCommand(
                    job_id,
                    {
                        "schema_version": "1.0",
                        "phase": "analyser",
                        "state": "inconsistent",
                        "completed": 0,
                        "total": 3,
                        "unit": "stage",
                    },
                )
            )
        except Exception:  # noqa: BLE001, S110 - preserve the primary failure
            pass
    message = error_summary(error) or type(error).__name__
    application.jobs.mark_failed(FailAttemptCommand(attempt_id, message))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Execute one CutMaster Material Analysis Job"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    add_supervisor_lease_arguments(parser)
    args = parser.parse_args(argv)
    application = CutMasterApplication.open(args.config)
    job_id = JobId.parse(args.job_id)
    exact_supervisor_grant = all(
        value is not None
        for value in (
            args.attempt_id,
            args.lease_worker_id,
            args.lease_process_id,
        )
    )
    with application.data_root_coordinator.shared(
        allow_maintenance=exact_supervisor_grant
    ):
        claimed = adopt_supervisor_lease(
            application,
            job_id=job_id,
            attempt_id=args.attempt_id,
            lease_worker_id=args.lease_worker_id,
            lease_process_id=args.lease_process_id,
            worker_kind="material",
        )
        status = execute_material_job(
            application,
            job_id,
            claimed_submission=claimed,
        )
    return (
        0 if status in {None, AttemptStatus.COMPLETE, AttemptStatus.INTERRUPTED} else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["MaterialAnalyser", "execute_material_job", "main"]
