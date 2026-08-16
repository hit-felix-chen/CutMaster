"""Execute one durable ASTER planning Job outside the Web server process."""

from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from threading import Event, Thread
from typing import Any
from uuid import uuid4

from cutmaster.application import CutMasterApplication
from cutmaster.application.direct import DirectService, PlanCommand
from cutmaster.application.jobs import (
    ClaimJobCommand,
    FailAttemptCommand,
    HeartbeatJobCommand,
)
from cutmaster.application.runs import CompleteRunCommand, RunView
from cutmaster.configuration.effective import (
    EffectiveConfiguration,
    SecretReferences,
)
from cutmaster.domain.attempts import TERMINAL_ATTEMPT_STATUSES
from cutmaster.domain.ids import AttemptId, JobId, RunId
from cutmaster.domain.runs import RunStatus
from cutmaster.workflow.ports import ProgressReporter, ProgressUpdate


RunPlanner = Callable[[CutMasterApplication, RunView, Path], Path]

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
) -> RunStatus | None:
    """Claim and execute exactly one queued ASTER planning Job.

    ``planner`` is injectable so transport tests exercise the durable state
    machine without contacting model providers. Production uses the same
    synchronous Direct planning service as the CLI and Benchmark adapters.
    """

    if not isinstance(application, CutMasterApplication):
        raise TypeError("application must be a CutMasterApplication")
    if not isinstance(job_id, JobId):
        raise TypeError("job_id must be a JobId")
    if heartbeat_interval_sec <= 0:
        raise ValueError("heartbeat_interval_sec must be positive")
    resolved_process_id = os.getpid() if process_id is None else process_id
    claimed = application.jobs.claim_next(
        ClaimJobCommand(
            worker_id or f"web-run-{resolved_process_id}",
            resolved_process_id,
            job_id,
        )
    )
    if claimed is None:
        return None
    attempt = claimed.attempt
    if attempt.operation_type != "aster_planning" or attempt.owner_type != "run":
        application.jobs.mark_failed(
            FailAttemptCommand(
                attempt.attempt_id,
                "Web Run worker received a non-ASTER planning Job",
            )
        )
        return RunStatus.FAILED

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
    published_plan: Path | None = None
    try:
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
            source_plan = (
                _plan_run(
                    application,
                    run,
                    workspace,
                    progress_reporter=ASTERJobProgressReporter(application, job_id),
                )
                if planner is None
                else planner(application, run, workspace)
            )
            relative_plan = _run_plan_relative_path(run)
            published_plan = _publish_plan(
                source_plan,
                application.settings.effective_configuration.data_root,
                relative_plan,
            )

        latest_job = application.jobs.get_job(job_id)
        if latest_job.stop_requested:
            published_plan.unlink(missing_ok=True)
            application.jobs.mark_interrupted(attempt.attempt_id)
            return RunStatus.INTERRUPTED

        completed = application.runs.complete(
            CompleteRunCommand(
                str(uuid4()),
                run_id,
                attempt.attempt_id,
                relative_plan,
            )
        )
        return completed.run.status
    except Exception as error:
        if published_plan is not None:
            published_plan.unlink(missing_ok=True)
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
) -> Path:
    if len(run.video_material_ids) != 1 or len(run.music_material_ids) != 1:
        raise ValueError("ASTER Run snapshot requires exactly one video and one music")
    video = application.materials.get(run.video_material_ids[0])
    music = application.materials.get(run.music_material_ids[0])
    if video is None or music is None:
        raise FileNotFoundError("An ASTER Run snapshot Material is unavailable")
    current = application.settings.effective_configuration
    snapshot = EffectiveConfiguration(
        sources=current.sources,
        data_root=current.data_root,
        secret_references=_snapshot_secret_references(run.configuration),
        _values=run.configuration,
    )
    result = DirectService(snapshot, application.materials).plan(
        PlanCommand(
            prompt=run.creative_brief.editing_intent,
            output_dir=workspace,
            video_material=video.name,
            music_material=music.name,
            target_output_length_sec=run.creative_brief.target_duration_sec,
            progress_reporter=progress_reporter,
        )
    )
    return result.render_plan_path


def _snapshot_secret_references(
    values: Mapping[str, Any],
) -> SecretReferences:
    def reference(*sections: str) -> str | None:
        value: Any = values
        for section in sections:
            if not isinstance(value, Mapping):
                return None
            value = value.get(section)
        if not isinstance(value, Mapping):
            return None
        raw = value.get("api_key_env")
        return None if raw is None else str(raw)

    return SecretReferences(
        llm_api_key_env=reference("llm"),
        vlm_api_key_env=reference("vlm"),
        asr_api_key_env=reference("analyser", "asr"),
    )


def _run_plan_relative_path(run: RunView) -> str:
    return f"projects/{run.project_id}/runs/{run.run_id}/plan.json"


def _publish_plan(source: Path, data_root: Path, relative_path: str) -> Path:
    resolved_source = source.resolve(strict=True)
    if not resolved_source.is_file() or resolved_source.is_symlink():
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
        with os.fdopen(descriptor, "wb") as destination, resolved_source.open(
            "rb"
        ) as source_stream:
            shutil.copyfileobj(source_stream, destination)
            destination.flush()
            os.fsync(destination.fileno())
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def _heartbeat_loop(
    application: CutMasterApplication,
    job_id: JobId,
    stop: Event,
    interval_sec: float,
) -> None:
    while not stop.wait(interval_sec):
        try:
            application.jobs.heartbeat(
                HeartbeatJobCommand(job_id)
            )
        except Exception:
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
        message = str(error).strip() or type(error).__name__
        application.jobs.mark_failed(FailAttemptCommand(attempt_id, message))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute one CutMaster Web Run Job")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    args = parser.parse_args(argv)
    application = CutMasterApplication.open(args.config)
    status = execute_run_job(application, JobId.parse(args.job_id))
    if status in {None, RunStatus.COMPLETE, RunStatus.INTERRUPTED}:
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess
    raise SystemExit(main())


__all__ = [
    "ASTERJobProgressReporter",
    "RunPlanner",
    "execute_run_job",
    "main",
]
