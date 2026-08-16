"""Launch durable managed Renderer jobs in isolated local subprocesses."""

from __future__ import annotations

import os
import subprocess
import sys
from threading import RLock, Thread
from typing import Protocol

from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import ClaimJobCommand, FailAttemptCommand
from cutmaster.application.renders import RenderSubmissionView
from cutmaster.domain.attempts import AttemptStatus, TERMINAL_ATTEMPT_STATUSES
from cutmaster.domain.ids import JobId
from cutmaster.infrastructure.observability.logging import error_summary


class RenderDispatcher(Protocol):
    def dispatch(self, submission: RenderSubmissionView) -> None: ...


class SubprocessRenderDispatcher:
    """Start one detached worker for each newly submitted Renderer Job."""

    __slots__ = ("_application", "_config_path", "_lock", "_processes")

    def __init__(self, application: CutMasterApplication) -> None:
        if not isinstance(application, CutMasterApplication):
            raise TypeError("application must be a CutMasterApplication")
        self._application = application
        self._config_path = application.settings.effective_configuration.sources.base_path
        self._lock = RLock()
        self._processes: dict[JobId, subprocess.Popen[bytes]] = {}

    def dispatch(self, submission: RenderSubmissionView) -> None:
        if submission.job is None or submission.attempt is None:
            return
        job_id = submission.job.job_id
        with self._lock:
            existing = self._processes.get(job_id)
            if existing is not None and existing.poll() is None:
                return
            # A replayed Idempotency-Key can deliver the same durable
            # submission again. Only a still-queued exact Job may cause a new
            # subprocess launch; active and terminal Jobs are wake-up no-ops.
            current = self._application.jobs.get_job(job_id)
            if current.status is not AttemptStatus.QUEUED:
                return
            log_path = (
                self._application.settings.effective_configuration.data_root
                / "logs"
                / "jobs"
                / f"{job_id}.log"
            )
            log_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with log_path.open("ab", buffering=0) as stream:
                    process = subprocess.Popen(
                        (
                            sys.executable,
                            "-m",
                            "cutmaster.adapters.web.render_worker",
                            "--config",
                            str(self._config_path),
                            "--job-id",
                            str(job_id),
                        ),
                        cwd=self._config_path.parent,
                        stdin=subprocess.DEVNULL,
                        stdout=stream,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
            except OSError as error:
                self._mark_launch_failed(submission, error)
                raise RuntimeError("Unable to launch the Renderer worker") from error
            self._processes[job_id] = process
            Thread(
                target=self._reap,
                args=(job_id, process),
                name=f"cutmaster-render-reaper-{job_id}",
                daemon=True,
            ).start()

    def _reap(self, job_id: JobId, process: subprocess.Popen[bytes]) -> None:
        return_code = process.wait()
        with self._lock:
            if self._processes.get(job_id) is process:
                self._processes.pop(job_id, None)
        if return_code == 0:
            return
        try:
            job = self._application.jobs.get_job(job_id)
            attempt = self._application.jobs.get_attempt(job.attempt_id)
            if attempt.status in TERMINAL_ATTEMPT_STATUSES:
                return
            if job.stop_requested:
                self._application.jobs.mark_interrupted(attempt.attempt_id)
            else:
                self._application.jobs.mark_failed(
                    FailAttemptCommand(
                        attempt.attempt_id,
                        f"Renderer worker exited with status {return_code}",
                    )
                )
        except Exception:
            return

    def _mark_launch_failed(
        self,
        submission: RenderSubmissionView,
        error: OSError,
    ) -> None:
        assert submission.job is not None
        claimed = self._application.jobs.claim_next(
            ClaimJobCommand(
                f"web-render-supervisor-{os.getpid()}",
                os.getpid(),
                submission.job.job_id,
            )
        )
        if claimed is not None:
            self._application.jobs.mark_failed(
                FailAttemptCommand(
                    claimed.attempt.attempt_id,
                    "Unable to launch Renderer worker: "
                    f"{error_summary(error) or type(error).__name__}",
                )
            )


__all__ = ["RenderDispatcher", "SubprocessRenderDispatcher"]
