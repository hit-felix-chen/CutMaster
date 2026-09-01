"""Execute one durable Material Analysis Job inside the Application boundary."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from threading import Event, RLock, Thread

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
    ProgressReporter,
    ProgressUpdate,
    WorkflowCancelledError,
)
from cutmaster.workflow.contracts import (
    AnalysisWorkspace,
    MUSIC_ANALYSIS_NODE_IDS,
    VIDEO_ANALYSIS_NODE_IDS,
)

MaterialAnalyser = Callable[[CutMasterApplication, MaterialView, Path], None]

class MaterialJobProgressReporter(ProgressReporter):
    """Persist node and per-node work progress for one Material analysis Job."""

    def __init__(
        self,
        application: CutMasterApplication,
        job_id: JobId,
        material: MaterialView,
    ) -> None:
        self._application = application
        self._job_id = job_id
        self._material_type = material.material_type.value
        node_ids = (
            VIDEO_ANALYSIS_NODE_IDS
            if self._material_type == "video"
            else MUSIC_ANALYSIS_NODE_IDS
        )
        self._nodes = {
            node_id: {
                "id": node_id,
                "state": "queued",
                "completed": 0,
                "total": 1,
                "unit": "task",
            }
            for node_id in node_ids
        }
        self._state = "preparing"
        self._lock = RLock()

    def set_state(self, state: str) -> None:
        if not state.strip():
            raise ValueError("Material analysis state must not be empty")
        with self._lock:
            self._state = state
            self._persist_locked()

    def report(self, update: ProgressUpdate) -> None:
        if update.total is None:
            raise ValueError("Material node progress must have a finite total")
        with self._lock:
            if update.description not in self._nodes:
                raise ValueError(
                    f"Unknown Material analysis node: {update.description!r}"
                )
            self._nodes[update.description] = {
                "id": update.description,
                "state": (
                    "complete"
                    if update.completed >= update.total
                    else "running"
                ),
                "completed": update.completed,
                "total": update.total,
                "unit": update.unit,
            }
            self._persist_locked()

    def complete_all(self) -> None:
        with self._lock:
            for node in self._nodes.values():
                node["state"] = "complete"
                node["completed"] = node["total"]
            self._persist_locked()

    def _persist_locked(self) -> None:
        nodes = [dict(node) for node in self._nodes.values()]
        completed = sum(node["state"] == "complete" for node in nodes)
        active_node = next(
            (node["id"] for node in nodes if node["state"] == "running"),
            None,
        )
        self._application.jobs.heartbeat(
            HeartbeatJobCommand(
                self._job_id,
                {
                    "schema_version": "2.0",
                    "phase": "analyser",
                    "material_type": self._material_type,
                    "state": self._state,
                    "completed": completed,
                    "total": len(nodes),
                    "unit": "node",
                    "active_node": active_node,
                    "nodes": nodes,
                },
            )
        )


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
    cancellation_token = DatabaseJobCancellationToken(application.jobs, job_id)
    progress_reporter = MaterialJobProgressReporter(application, job_id, material)

    stop_heartbeat = Event()
    heartbeat = Thread(
        target=_heartbeat_loop,
        args=(application, job_id, stop_heartbeat, heartbeat_interval_sec),
        name=f"cutmaster-material-heartbeat-{job_id}",
        daemon=True,
    )
    heartbeat.start()
    try:
        progress_reporter.set_state("preparing")
        cancellation_token.raise_if_cancelled()
        with tempfile.TemporaryDirectory(prefix=f"cutmaster-{material_id}-") as raw:
            workspace = Path(raw).resolve()
            progress_reporter.set_state("analysing")
            if analyser is None:
                _analyse_material(
                    application,
                    material,
                    workspace,
                    progress_reporter=progress_reporter,
                    cancellation_token=cancellation_token,
                )
            else:
                cancellation_token.raise_if_cancelled()
                analyser(application, material, workspace)
                current = application.materials.get(material.material_id)
                if current is None or current.condition is not MaterialCondition.READY:
                    cancellation_token.raise_if_cancelled()
        progress_reporter.complete_all()
        progress_reporter.set_state("publishing")
        progress_reporter.set_state("complete")
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
            progress_reporter=progress_reporter,
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
    progress_reporter: ProgressReporter | None = None,
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
            progress_reporter=progress_reporter,
        ),
        cancellation_token=cancellation_token,
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
    *,
    progress_reporter: MaterialJobProgressReporter | None = None,
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
            if progress_reporter is not None:
                progress_reporter.set_state("inconsistent")
        except Exception:  # noqa: BLE001, S110 - preserve the primary failure
            pass
    message = error_summary(error) or type(error).__name__
    application.jobs.mark_failed(FailAttemptCommand(attempt_id, message))


__all__ = [
    "MaterialAnalyser",
    "MaterialJobProgressReporter",
    "execute_material_job",
]
