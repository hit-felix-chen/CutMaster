"""Execute one durable Material Analysis Job inside the Application boundary."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import Event, Thread

from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import (
    ClaimJobCommand,
    FailAttemptCommand,
    HeartbeatJobCommand,
    JobSubmissionView,
)
from cutmaster.application.jobs.cancellation import DatabaseJobCancellationToken
from cutmaster.application.jobs.lease import validate_claimed_submission
from cutmaster.application.materials import (
    ExecuteMaterialAnalysisCommand,
    ManagedMaterialAnalysisExecutor,
    MaterialView,
)
from cutmaster.domain.attempts import TERMINAL_ATTEMPT_STATUSES, AttemptStatus
from cutmaster.domain.ids import AttemptId, JobId, MaterialId
from cutmaster.domain.materials import MaterialCondition
from cutmaster.infrastructure.storage.local.material_catalog import (
    MaterialInconsistentError,
)
from cutmaster.infrastructure.observability.logging import error_summary
from cutmaster.workflow.ports import (
    CancellationToken,
    WorkflowCancelledError,
)
from cutmaster.workflow.contracts import AnalysisWorkspace

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
                worker_id or f"managed-material-{resolved_process_id}",
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
    ManagedMaterialAnalysisExecutor(
        application.settings.effective_configuration,
        application.materials,
    ).execute(
        ExecuteMaterialAnalysisCommand(
            material_id=material.material_id,
            workspace=AnalysisWorkspace(workspace),
            video_title=material.name,
            material_reused=material.reused,
        ),
        cancellation_token=cancellation_token,
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


__all__ = ["MaterialAnalyser", "execute_material_job"]
