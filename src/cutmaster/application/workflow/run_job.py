"""Execute one durable ASTER planning Job inside the Application boundary."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Event, Thread
from typing import TYPE_CHECKING, Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import (
    ClaimJobCommand,
    FailAttemptCommand,
    HeartbeatJobCommand,
    JobSubmissionView,
    RecordAttemptUsageCommand,
)
from cutmaster.application.jobs.cancellation import DatabaseJobCancellationToken
from cutmaster.application.jobs.lease import validate_claimed_submission
from cutmaster.application.jobs.usage import (
    normalize_usage_summary,
)
from cutmaster.application.runs import (
    CompleteRunCommand,
    ExecuteRunPlanningCommand,
    RunPlanningArtifacts,
    RunPlanningExecutor,
    RunView,
)
from cutmaster.application.runs.review import (
    REVIEW_BUNDLE_FILENAME,
    REVIEW_BUNDLE_SCHEMA_VERSION,
    artifact_manifest_entry,
    write_json_atomic,
)
from cutmaster.domain.attempts import TERMINAL_ATTEMPT_STATUSES
from cutmaster.domain.ids import AttemptId, JobId, RunId
from cutmaster.domain.runs import RunStatus
from cutmaster.infrastructure.observability.logging import error_summary
from cutmaster.workflow.ports import (
    CancellationToken,
    ProgressReporter,
    ProgressUpdate,
    WorkflowCancelledError,
    raise_if_cancelled,
)

if TYPE_CHECKING:
    from cutmaster.workflow.contracts.checkpoints import PlannersCheckpointStore

RunPlanner = Callable[[CutMasterApplication, RunView, Path], Path]
PreviewDispatcher = Callable[[object], None]

LOGGER = logging.getLogger(__name__)

_ASTER_AGENTS = (
    "arrangement_architect",
    "story_editor",
    "timeline_scout",
    "edit_composer",
    "revision_editor",
)


class ASTERJobProgressReporter(ProgressReporter):
    """Persist real ASTER agent milestones in the durable Job snapshot."""

    def __init__(self, application: CutMasterApplication, job_id: JobId) -> None:
        self._application = application
        self._job_id = job_id

    def report(self, update: ProgressUpdate) -> None:
        if update.total != len(_ASTER_AGENTS) or update.unit != "agent":
            raise ValueError("ASTER progress must report the five agent milestones")
        try:
            active_index = _ASTER_AGENTS.index(update.description)
        except ValueError as error:
            raise ValueError(
                f"Unknown ASTER progress agent: {update.description!r}"
            ) from error
        complete = update.completed == len(_ASTER_AGENTS)
        milestones = []
        for index, agent in enumerate(_ASTER_AGENTS):
            if complete or index < active_index:
                state = "complete"
            elif index == active_index:
                state = "running"
            else:
                state = "queued"
            milestones.append({"id": agent, "agent": agent, "state": state})
        self._application.jobs.heartbeat(
            HeartbeatJobCommand(
                self._job_id,
                {
                    "schema_version": "1.0",
                    "phase": "planners",
                    "agent": update.description,
                    "state": "complete" if complete else "running",
                    "completed": update.completed,
                    "total": update.total,
                    "unit": update.unit,
                    "milestones": milestones,
                },
            )
        )


def execute_run_job(
    application: CutMasterApplication,
    job_id: JobId,
    *,
    planner: RunPlanner | None = None,
    worker_id: str | None = None,
    process_id: int | None = None,
    heartbeat_interval_sec: float = 10.0,
    preview_dispatcher: PreviewDispatcher | None = None,
    claimed_submission: JobSubmissionView | None = None,
) -> RunStatus | None:
    """Claim and execute exactly one queued ASTER planning Job.

    ``planner`` is injectable so transport tests exercise the durable state
    machine without contacting model providers. Production delegates to the
    same Application planning executor used by every inbound adapter.
    """

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
                worker_id or f"managed-run-{resolved_process_id}",
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
    if attempt.operation_type != "aster_planning" or attempt.owner_type != "run":
        application.jobs.mark_failed(
            FailAttemptCommand(
                attempt.attempt_id,
                "Managed Run executor received a non-ASTER planning Job",
            )
        )
        return RunStatus.FAILED
    cancellation_token = DatabaseJobCancellationToken(application.jobs, job_id)

    run_id = RunId.parse(attempt.owner_id)
    run = application.runs.get(run_id)
    stop_heartbeat = Event()
    heartbeat = Thread(
        target=_heartbeat_loop,
        args=(application, job_id, stop_heartbeat, heartbeat_interval_sec),
        name=f"cutmaster-heartbeat-{job_id}",
        daemon=True,
    )
    heartbeat.start()
    published_paths: list[Path] = []
    attempt_usage: dict[str, Any] | None = None
    checkpoint_session = None
    try:
        cancellation_token.raise_if_cancelled()
        if planner is None:
            checkpoint_session = application.runs.checkpoint_session(
                run_id,
                attempt.attempt_id,
            )
        application.jobs.heartbeat(
            HeartbeatJobCommand(
                job_id,
                {
                    "schema_version": "1.0",
                    "phase": "planners",
                    "agent": None,
                    "state": "preparing",
                    "completed": 0,
                    "total": len(_ASTER_AGENTS),
                    "unit": "agent",
                    "milestones": [
                        {"id": agent, "agent": agent, "state": "queued"}
                        for agent in _ASTER_AGENTS
                    ],
                },
            )
        )
        with tempfile.TemporaryDirectory(prefix=f"cutmaster-{run_id}-") as raw:
            workspace = Path(raw).resolve()
            planned: Path | RunPlanningArtifacts | None = None
            try:
                if planner is None:
                    planned = _plan_run(
                        application,
                        run,
                        workspace,
                        progress_reporter=ASTERJobProgressReporter(
                            application,
                            job_id,
                        ),
                        cancellation_token=cancellation_token,
                        checkpoint_store=checkpoint_session,
                    )
                else:
                    cancellation_token.raise_if_cancelled()
                    planned = planner(application, run, workspace)
                    cancellation_token.raise_if_cancelled()
                relative_plan = _run_plan_relative_path(run)
                source_plan = (
                    planned.render_plan
                    if isinstance(planned, RunPlanningArtifacts)
                    else planned
                )
                cancellation_token.raise_if_cancelled()
                published_plan = _publish_plan(
                    source_plan,
                    application.settings.effective_configuration.data_root,
                    relative_plan,
                )
                published_paths.append(published_plan)
                cancellation_token.raise_if_cancelled()
                if isinstance(planned, RunPlanningArtifacts):
                    published_paths.extend(
                        _publish_review_bundle(
                            planned,
                            published_plan.parent,
                            cancellation_token=cancellation_token,
                        )
                    )
                cancellation_token.raise_if_cancelled()
            finally:
                # The temporary planners workspace is the only source for
                # partial-call usage when planning raises or is cancelled.
                attempt_usage = _capture_attempt_usage(workspace, planned)

        latest_job = application.jobs.get_job(job_id)
        if latest_job.stop_requested:
            for path in reversed(published_paths):
                path.unlink(missing_ok=True)
            _record_attempt_usage_best_effort(
                application,
                attempt.attempt_id,
                attempt_usage,
            )
            application.jobs.mark_interrupted(attempt.attempt_id)
            return RunStatus.INTERRUPTED

        completed = application.runs.complete(
            CompleteRunCommand(
                str(uuid4()),
                run_id,
                attempt.attempt_id,
                relative_plan,
                attempt_usage,
            )
        )
        if checkpoint_session is not None:
            try:
                checkpoint_session.clear()
            except Exception:  # noqa: BLE001 - completed Run stays authoritative
                LOGGER.exception("Unable to clean completed ASTER checkpoint")
        if preview_dispatcher is not None:
            _create_and_dispatch_preview(
                application,
                completed.frozen_edit.edit_id,
                preview_dispatcher,
            )
        return completed.run.status
    except WorkflowCancelledError:
        for path in reversed(published_paths):
            path.unlink(missing_ok=True)
        _record_attempt_usage_best_effort(
            application,
            attempt.attempt_id,
            attempt_usage,
        )
        attempt_view = application.jobs.get_attempt(attempt.attempt_id)
        if attempt_view.status not in TERMINAL_ATTEMPT_STATUSES:
            application.jobs.mark_interrupted(attempt.attempt_id)
        return application.runs.get(run_id).status
    except Exception as error:  # noqa: BLE001 - worker failure boundary
        for path in reversed(published_paths):
            path.unlink(missing_ok=True)
        _record_attempt_usage_best_effort(
            application,
            attempt.attempt_id,
            attempt_usage,
        )
        _finish_failed_or_interrupted(application, job_id, attempt.attempt_id, error)
        return application.runs.get(run_id).status
    finally:
        stop_heartbeat.set()
        heartbeat.join(timeout=min(heartbeat_interval_sec, 1.0))


def _plan_run(
    application: CutMasterApplication,
    run: RunView,
    workspace: Path,
    *,
    progress_reporter: ProgressReporter | None = None,
    cancellation_token: CancellationToken | None = None,
    checkpoint_store: PlannersCheckpointStore | None = None,
) -> RunPlanningArtifacts:
    return RunPlanningExecutor(
        application.settings.effective_configuration,
        application.materials,
    ).execute(
        ExecuteRunPlanningCommand(
            run=run,
            workspace=workspace.resolve(),
            progress_reporter=progress_reporter,
            cancellation_token=cancellation_token,
            checkpoint_store=checkpoint_store,
        )
    )


def _capture_attempt_usage(
    workspace: Path,
    planned: Path | RunPlanningArtifacts | None,
) -> dict[str, Any] | None:
    """Capture only this planners invocation's aggregate before cleanup."""

    try:
        if isinstance(planned, RunPlanningArtifacts):
            candidate = planned.model_usage_summary
        else:
            # This import is intentionally on the executing worker path: the
            # module owns provider clients and must not make Application.open
            # eagerly import the OpenAI stack.
            from cutmaster.infrastructure.models.openai_compatible import (
                load_usage_summary,
            )

            usage_path = workspace / "diagnostics" / "model_usage.json"
            if not usage_path.is_file():
                return None
            payload = json.loads(usage_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema_version") != "2.0":
                raise ValueError("ASTER usage artifact must use schema 2.0")
            current_run = payload.get("current_run")
            if not isinstance(current_run, dict) or not isinstance(
                current_run.get("summary"),
                dict,
            ):
                raise ValueError("ASTER usage artifact has no current_run summary")
            candidate = load_usage_summary(
                usage_path,
                cumulative=False,
            )
        return normalize_usage_summary(candidate)
    except Exception:  # noqa: BLE001 - telemetry cannot mask the Run outcome
        LOGGER.exception("Unable to capture ASTER Attempt model usage")
        return None


def _record_attempt_usage_best_effort(
    application: CutMasterApplication,
    attempt_id: AttemptId,
    model_usage_summary: Mapping[str, Any] | None,
) -> None:
    if model_usage_summary is None:
        return
    try:
        application.jobs.record_attempt_usage(
            RecordAttemptUsageCommand(attempt_id, model_usage_summary)
        )
    except Exception:  # noqa: BLE001 - preserve the original terminal outcome
        LOGGER.exception("Unable to persist ASTER Attempt model usage")


def _run_plan_relative_path(run: RunView) -> str:
    return f"projects/{run.project_id}/runs/{run.run_id}/plan.json"


def _publish_plan(source: Path, data_root: Path, relative_path: str) -> Path:
    if source.is_symlink():
        raise ValueError("Run planner produced a symlinked RenderPlan")
    resolved_source = source.resolve(strict=True)
    if not resolved_source.is_file():
        raise ValueError("Run planner did not produce a regular RenderPlan file")
    resolved_root = data_root.resolve()
    target = resolved_root.joinpath(*relative_path.split("/"))
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.resolve() != target.parent or target.parent.is_symlink():
        raise ValueError("Managed Run artifact directory is unsafe")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with (
            os.fdopen(descriptor, "wb") as destination,
            resolved_source.open("rb") as source_stream,
        ):
            shutil.copyfileobj(source_stream, destination)
            destination.flush()
            os.fsync(destination.fileno())
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def _publish_review_bundle(
    artifacts: RunPlanningArtifacts,
    target_directory: Path,
    *,
    cancellation_token: CancellationToken | None = None,
) -> list[Path]:
    """Publish the immutable Review inputs and manifest commit point."""

    sources = {
        "candidate_pool": artifacts.candidate_pool,
        "edit_plan": artifacts.edit_plan,
        "dialogue_anchors": artifacts.dialogue_anchors,
        "raw_script": artifacts.raw_script,
        "music_profile": artifacts.music_profile,
        "selection_diagnostics": artifacts.selection_diagnostics,
    }
    published: list[Path] = []
    try:
        entries: dict[str, dict[str, str]] = {}
        for logical_name, source in sources.items():
            raise_if_cancelled(cancellation_token)
            if source.is_symlink() or not source.is_file():
                raise ValueError(
                    f"Run planner did not produce a regular {logical_name} artifact"
                )
            digest = _file_sha256(source)
            target = _publish_sibling(
                source,
                target_directory,
                f"{logical_name}.{digest[:16]}.json",
            )
            published.append(target)
            entries[logical_name] = artifact_manifest_entry(target)
            raise_if_cancelled(cancellation_token)
        raise_if_cancelled(cancellation_token)
        manifest = target_directory / REVIEW_BUNDLE_FILENAME
        write_json_atomic(
            manifest,
            {
                "schema_version": REVIEW_BUNDLE_SCHEMA_VERSION,
                "artifacts": entries,
            },
        )
        published.append(manifest)
        return published
    except Exception:
        for path in reversed(published):
            path.unlink(missing_ok=True)
        raise


def _publish_sibling(source: Path, directory: Path, name: str) -> Path:
    if source.is_symlink():
        raise ValueError(f"Run planner produced a symlinked {name}")
    resolved = source.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"Run planner did not produce {name}")
    target = directory / name
    descriptor, raw_temporary = tempfile.mkstemp(
        dir=directory,
        prefix=f".{name}.",
        suffix=".tmp",
    )
    temporary = Path(raw_temporary)
    try:
        with (
            os.fdopen(descriptor, "wb") as destination,
            resolved.open("rb") as source_stream,
        ):
            shutil.copyfileobj(source_stream, destination)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _create_and_dispatch_preview(
    application: CutMasterApplication,
    edit_id,
    dispatcher: PreviewDispatcher,
) -> None:
    try:
        command_id = str(uuid5(NAMESPACE_URL, f"cutmaster:dialogue-preview:{edit_id}"))
        submission = application.renders.create_dialogue_preview(command_id, edit_id)
        if submission.created:
            dispatcher(submission)
    except Exception:
        # Run completion is already committed and remains authoritative. The
        # Preview owns an independent failure lifecycle.
        LOGGER.exception("Unable to create or dispatch the default dialogue Preview")


__all__ = [
    "ASTERJobProgressReporter",
    "RunPlanner",
    "execute_run_job",
]
