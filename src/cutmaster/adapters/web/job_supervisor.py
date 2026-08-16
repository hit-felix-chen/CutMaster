"""Durable FIFO supervisor for every managed local Web job."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, RLock, Thread
from typing import Protocol
from uuid import uuid4

from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import (
    ClaimSupervisedJobCommand,
    FailAttemptCommand,
    InterruptOrphansCommand,
    JobSubmissionView,
)
from cutmaster.application.ports.data_root import DataRootUnavailableError
from cutmaster.domain.attempts import TERMINAL_ATTEMPT_STATUSES
from cutmaster.domain.ids import JobId
from cutmaster.infrastructure.observability.logging import error_summary


class ManagedProcess(Protocol):
    def poll(self) -> int | None: ...

    def wait(self) -> int: ...


class JobSupervisor(Protocol):
    """Lifecycle-aware durable queue consumer used by the Web adapter."""

    def dispatch(self, submission: object) -> None: ...

    def start(self) -> None: ...

    def stop(self, *, timeout_sec: float = 5.0) -> None: ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...


ProcessFactory = Callable[..., ManagedProcess]
Clock = Callable[[], datetime]

_WORKER_MODULES = {
    "material_analysis": "cutmaster.adapters.web.material_worker",
    "aster_planning": "cutmaster.adapters.web.run_worker",
    "rendering": "cutmaster.adapters.web.render_worker",
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LocalJobSupervisor:
    """Consume one durable queue with global capacity and owner serialization."""

    __slots__ = (
        "_application",
        "_clock",
        "_config_path",
        "_lock",
        "_max_concurrency",
        "_orphan_after_sec",
        "_orphan_audit_interval_sec",
        "_poll_interval_sec",
        "_process_factory",
        "_processes",
        "_manual_pause",
        "_shutdown",
        "_thread",
        "_wake",
        "_worker_id",
    )

    def __init__(
        self,
        application: CutMasterApplication,
        *,
        max_concurrency: int = 2,
        poll_interval_sec: float = 0.25,
        orphan_after_sec: float = 45.0,
        orphan_audit_interval_sec: float = 10.0,
        process_factory: ProcessFactory = subprocess.Popen,
        clock: Clock = _utc_now,
    ) -> None:
        if not isinstance(application, CutMasterApplication):
            raise TypeError("application must be a CutMasterApplication")
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or max_concurrency <= 0
        ):
            raise ValueError("max_concurrency must be a positive integer")
        for value, label in (
            (poll_interval_sec, "poll_interval_sec"),
            (orphan_after_sec, "orphan_after_sec"),
            (orphan_audit_interval_sec, "orphan_audit_interval_sec"),
        ):
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value <= 0
            ):
                raise ValueError(f"{label} must be positive")
        self._application = application
        self._max_concurrency = max_concurrency
        self._poll_interval_sec = float(poll_interval_sec)
        self._orphan_after_sec = float(orphan_after_sec)
        self._orphan_audit_interval_sec = float(orphan_audit_interval_sec)
        self._process_factory = process_factory
        self._clock = clock
        self._worker_id = f"web-supervisor-{os.getpid()}-{uuid4()}"
        self._lock = RLock()
        self._wake = Event()
        self._shutdown: Event | None = None
        self._thread: Thread | None = None
        self._config_path: Path | None = None
        self._processes: dict[JobId, ManagedProcess] = {}
        self._manual_pause = Event()

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def active_process_count(self) -> int:
        """Return children launched by this process and not reaped yet."""

        with self._lock:
            return len(self._processes)

    def dispatch(self, _submission: object) -> None:
        """Wake the durable consumer; the submission is already persisted."""

        self._wake.set()

    def pause(self) -> None:
        """Stop new durable claims without changing queued or active Jobs."""

        self._manual_pause.set()
        self._wake.set()

    def resume(self) -> None:
        self._manual_pause.clear()
        self._wake.set()

    def start(self) -> None:
        """Audit stale leases and start consuming queued Jobs."""

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            configuration = self._application.settings.effective_configuration
            config_path = configuration.sources.base_path
            if not self._application.data_root_coordinator.state().maintenance:
                self._interrupt_stale_jobs()
            shutdown = Event()
            thread = Thread(
                target=self._run,
                args=(shutdown,),
                name="cutmaster-job-supervisor",
                daemon=True,
            )
            self._config_path = config_path
            self._shutdown = shutdown
            self._thread = thread
            self._wake.set()
            thread.start()

    def stop(self, *, timeout_sec: float = 5.0) -> None:
        """Stop scheduling without terminating or rewriting active children."""

        if timeout_sec <= 0:
            raise ValueError("timeout_sec must be positive")
        with self._lock:
            shutdown = self._shutdown
            thread = self._thread
        if shutdown is None or thread is None:
            return
        shutdown.set()
        self._wake.set()
        thread.join(timeout=timeout_sec)
        with self._lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None
                self._shutdown = None

    def _run(self, shutdown: Event) -> None:
        next_audit = self._clock()
        while not shutdown.is_set():
            root_state = self._application.data_root_coordinator.state()
            if self._manual_pause.is_set() or root_state.maintenance:
                self._wake.wait(self._poll_interval_sec)
                self._wake.clear()
                continue
            now = self._clock()
            if now >= next_audit:
                self._interrupt_stale_jobs()
                next_audit = now + timedelta(seconds=self._orphan_audit_interval_sec)
            while not shutdown.is_set():
                try:
                    claimed = self._application.jobs.claim_next_supervised(
                        ClaimSupervisedJobCommand(
                            self._worker_id,
                            os.getpid(),
                            self._max_concurrency,
                        )
                    )
                except DataRootUnavailableError:
                    # Maintenance may win immediately after the state check.
                    # The durable Job remains queued; the supervisor stays
                    # alive and resumes naturally when authority is restored.
                    break
                if claimed is None:
                    break
                if shutdown.is_set():
                    # The durable claim won the race with lifespan shutdown,
                    # but no child has been started.  Interrupt it honestly;
                    # a later explicit Resume creates the next Attempt.
                    self._application.jobs.mark_interrupted(claimed.attempt.attempt_id)
                    break
                self._launch(claimed, shutdown)
            self._wake.wait(self._poll_interval_sec)
            self._wake.clear()

    def _interrupt_stale_jobs(self) -> None:
        now = self._clock()
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        self._application.jobs.interrupt_orphans(
            InterruptOrphansCommand(now - timedelta(seconds=self._orphan_after_sec))
        )

    def _launch(self, submission: JobSubmissionView, shutdown: Event) -> None:
        module = _WORKER_MODULES.get(submission.attempt.operation_type)
        if module is None:
            self._application.jobs.mark_failed(
                FailAttemptCommand(
                    submission.attempt.attempt_id,
                    "Supervisor received an unsupported Job operation",
                )
            )
            return
        config_path = self._config_path
        if config_path is None:  # pragma: no cover - guarded by start
            raise RuntimeError("Job Supervisor has no effective configuration")
        job_id = submission.job.job_id
        log_path = (
            self._application.settings.effective_configuration.data_root
            / "logs"
            / "jobs"
            / f"{job_id}.log"
        )
        command = (
            sys.executable,
            "-m",
            module,
            "--config",
            str(config_path),
            "--job-id",
            str(job_id),
            "--attempt-id",
            str(submission.attempt.attempt_id),
            "--lease-worker-id",
            self._worker_id,
            "--lease-process-id",
            str(os.getpid()),
        )
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("ab", buffering=0) as stream:
                process = self._process_factory(
                    command,
                    cwd=config_path.parent,
                    stdin=subprocess.DEVNULL,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    start_new_session=True,
                )
        except OSError as error:
            self._application.jobs.mark_failed(
                FailAttemptCommand(
                    submission.attempt.attempt_id,
                    "Unable to launch managed worker: "
                    f"{error_summary(error) or type(error).__name__}",
                )
            )
            self._wake.set()
            return
        with self._lock:
            self._processes[job_id] = process
        Thread(
            target=self._reap,
            args=(submission, process, shutdown),
            name=f"cutmaster-job-reaper-{job_id}",
            daemon=True,
        ).start()

    def _reap(
        self,
        submission: JobSubmissionView,
        process: ManagedProcess,
        shutdown: Event,
    ) -> None:
        return_code = process.wait()
        job_id = submission.job.job_id
        with self._lock:
            if self._processes.get(job_id) is process:
                self._processes.pop(job_id, None)
        if shutdown.is_set():
            return
        try:
            attempt = self._application.jobs.get_attempt(submission.attempt.attempt_id)
            if attempt.status in TERMINAL_ATTEMPT_STATUSES:
                return
            job = self._application.jobs.get_job(job_id)
            if job.stop_requested:
                self._application.jobs.mark_interrupted(attempt.attempt_id)
            else:
                self._application.jobs.mark_failed(
                    FailAttemptCommand(
                        attempt.attempt_id,
                        f"Managed worker exited with status {return_code} "
                        "without completing its Attempt",
                    )
                )
        except Exception:  # noqa: BLE001 - best-effort reaping during shutdown
            # Startup orphan audit is authoritative if persistence is already
            # unavailable while the server itself is exiting.
            return
        finally:
            self._wake.set()


__all__ = [
    "JobSupervisor",
    "LocalJobSupervisor",
    "ManagedProcess",
    "ProcessFactory",
]
