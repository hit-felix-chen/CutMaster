from __future__ import annotations

import os
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Condition, Event
from time import monotonic, sleep
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cutmaster.adapters.web import create_app
from cutmaster.adapters.web.job_supervisor import LocalJobSupervisor
from cutmaster.adapters.web.worker_lease import adopt_supervisor_lease
from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import (
    ClaimJobCommand,
    ClaimSupervisedJobCommand,
    EnqueueMaterialAnalysisCommand,
    JobsService,
    StopAttemptCommand,
)
from cutmaster.domain.attempts import AttemptStatus
from cutmaster.domain.ids import AttemptId, JobId, MaterialId
from cutmaster.infrastructure.persistence.sqlite import (
    ManagedStateConflict,
    SQLiteApplicationStore,
)


def _command_id() -> str:
    return str(uuid4())


def _queue_material(
    application: CutMasterApplication,
    tmp_path: Path,
    name: str,
):
    source = tmp_path / f"{name}.mp4"
    source.write_bytes(name.encode())
    material = application.materials.add(source, "video", name)
    return application.jobs.enqueue_material_analysis(
        EnqueueMaterialAnalysisCommand(_command_id(), material.material_id)
    )


def _wait_until(predicate, *, timeout: float = 3.0) -> None:
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.01)
    raise AssertionError("condition was not satisfied before timeout")


class _BlockingProcess:
    def __init__(self) -> None:
        self._finished = Event()
        self.return_code = 0

    def finish(self, return_code: int = 0) -> None:
        self.return_code = return_code
        self._finished.set()

    def poll(self) -> int | None:
        return self.return_code if self._finished.is_set() else None

    def wait(self) -> int:
        assert self._finished.wait(timeout=5)
        return self.return_code


class _RecordingProcessFactory:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], dict[str, object]]] = []
        self.processes: list[_BlockingProcess] = []
        self._condition = Condition()

    def __call__(self, command, **kwargs):
        process = _BlockingProcess()
        with self._condition:
            self.calls.append((tuple(command), dict(kwargs)))
            self.processes.append(process)
            self._condition.notify_all()
        return process

    def wait_for_calls(self, count: int, timeout: float = 3.0) -> None:
        deadline = monotonic() + timeout
        with self._condition:
            while len(self.calls) < count:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise AssertionError(f"expected {count} process launches")
                self._condition.wait(timeout=remaining)


def _insert_queued_job(
    application: CutMasterApplication,
    operation_type: str,
    *,
    owner_id: MaterialId | None = None,
    created_at: datetime | None = None,
):
    root = application.settings.effective_configuration.data_root
    store = SQLiteApplicationStore(root)
    _ = store.schema_version
    attempt_id = AttemptId.new()
    job_id = JobId.new()
    material_id = owner_id or MaterialId.new()
    timestamp = (created_at or datetime.now(UTC)).isoformat()
    with sqlite3.connect(store.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        sequence = connection.execute(
            "SELECT coalesce(max(sequence), 0) + 1 FROM attempts WHERE material_id = ?",
            (str(material_id),),
        ).fetchone()[0]
        connection.execute(
            """
            INSERT INTO attempts (
                attempt_id, operation_type, material_id, run_id,
                render_variant_id, sequence, status, command_id, error_message,
                created_at, started_at, finished_at, updated_at
            ) VALUES (?, ?, ?, NULL, NULL, ?, 'queued', ?, NULL, ?, NULL, NULL, ?)
            """,
            (
                str(attempt_id),
                operation_type,
                str(material_id),
                sequence,
                _command_id(),
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO jobs (
                job_id, attempt_id, status, stop_requested, worker_id,
                process_id, heartbeat_at, progress_json, created_at, updated_at
            ) VALUES (?, ?, 'queued', 0, NULL, NULL, NULL, '{}', ?, ?)
            """,
            (str(job_id), str(attempt_id), timestamp, timestamp),
        )
    return attempt_id, job_id


def _set_heartbeat(
    application: CutMasterApplication,
    job_id: JobId,
    value: datetime,
) -> None:
    store = SQLiteApplicationStore(
        application.settings.effective_configuration.data_root
    )
    timestamp = value.isoformat()
    with sqlite3.connect(store.database_path) as connection:
        connection.execute(
            "UPDATE jobs SET heartbeat_at = ?, updated_at = ? WHERE job_id = ?",
            (timestamp, timestamp, str(job_id)),
        )


def test_supervised_claim_is_atomic_across_two_service_instances(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    queued = _queue_material(application, tmp_path, "atomic")
    configuration = application.settings.effective_configuration
    services = (JobsService(configuration), JobsService(configuration))
    barrier = Barrier(2)

    def claim(index: int):
        barrier.wait(timeout=3)
        return services[index].claim_next_supervised(
            ClaimSupervisedJobCommand(f"supervisor-{index}", 100 + index, 1)
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(claim, range(2)))

    claimed = [result for result in results if result is not None]
    assert len(claimed) == 1
    assert claimed[0].job.job_id == queued.job.job_id
    assert application.jobs.get_job(queued.job.job_id).status is AttemptStatus.RUNNING


def test_two_live_supervisors_never_launch_the_same_job(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    queued = _queue_material(application, tmp_path, "dual-supervisor")
    factories = (_RecordingProcessFactory(), _RecordingProcessFactory())
    supervisors = tuple(
        LocalJobSupervisor(
            application,
            max_concurrency=1,
            poll_interval_sec=0.01,
            orphan_after_sec=3600,
            process_factory=factory,
        )
        for factory in factories
    )

    with ThreadPoolExecutor(max_workers=2) as executor:
        tuple(executor.map(lambda item: item.start(), supervisors))
    try:
        _wait_until(lambda: sum(len(item.calls) for item in factories) == 1)
        sleep(0.05)
        assert sum(len(item.calls) for item in factories) == 1
        command = next(item.calls[0][0] for item in factories if item.calls)
        assert command[command.index("--job-id") + 1] == str(queued.job.job_id)
    finally:
        for supervisor in supervisors:
            supervisor.stop()
        for factory in factories:
            for process in factory.processes:
                process.finish()
        _wait_until(lambda: all(item.active_process_count == 0 for item in supervisors))


def test_supervised_claim_skips_owner_blocked_work_without_reordering_others(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    first = _queue_material(application, tmp_path, "owner-one")
    second = _queue_material(application, tmp_path, "owner-two")
    running = application.jobs.claim_next(
        ClaimJobCommand("external", 200, first.job.job_id)
    )
    assert running is not None
    blocked_attempt, blocked_job = _insert_queued_job(
        application,
        "material_analysis",
        owner_id=MaterialId.parse(first.attempt.owner_id),
        created_at=first.job.created_at - timedelta(days=1),
    )

    selected = application.jobs.claim_next_supervised(
        ClaimSupervisedJobCommand("supervisor", 201, 3)
    )

    assert selected is not None
    assert selected.job.job_id == second.job.job_id
    assert application.jobs.get_job(blocked_job).status is AttemptStatus.QUEUED
    assert application.jobs.get_attempt(blocked_attempt).status is AttemptStatus.QUEUED


def test_worker_can_only_adopt_the_exact_supervisor_lease(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    queued = _queue_material(application, tmp_path, "lease")
    claimed = application.jobs.claim_next_supervised(
        ClaimSupervisedJobCommand("lease-owner", 301, 1)
    )
    assert claimed is not None

    with pytest.raises(ManagedStateConflict, match="expected supervisor lease"):
        adopt_supervisor_lease(
            application,
            job_id=queued.job.job_id,
            attempt_id=str(queued.attempt.attempt_id),
            lease_worker_id="wrong-owner",
            lease_process_id=301,
            worker_kind="material",
        )

    adopted = adopt_supervisor_lease(
        application,
        job_id=queued.job.job_id,
        attempt_id=str(queued.attempt.attempt_id),
        lease_worker_id="lease-owner",
        lease_process_id=301,
        worker_kind="material",
    )
    assert adopted is not None
    assert adopted.job.worker_id == f"web-material-{os.getpid()}"
    assert adopted.job.process_id == os.getpid()
    with pytest.raises(ManagedStateConflict):
        adopt_supervisor_lease(
            application,
            job_id=queued.job.job_id,
            attempt_id=str(queued.attempt.attempt_id),
            lease_worker_id="lease-owner",
            lease_process_id=301,
            worker_kind="material",
        )


def test_supervisor_enforces_global_capacity_and_fifo(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    queued = [
        _queue_material(application, tmp_path, f"capacity-{index}")
        for index in range(3)
    ]
    factory = _RecordingProcessFactory()
    supervisor = LocalJobSupervisor(
        application,
        max_concurrency=2,
        poll_interval_sec=0.01,
        orphan_after_sec=3600,
        process_factory=factory,
    )

    supervisor.start()
    try:
        factory.wait_for_calls(2)
        assert len(factory.calls) == 2
        launched_ids = [
            call[0][call[0].index("--job-id") + 1] for call in factory.calls
        ]
        assert launched_ids == [str(item.job.job_id) for item in queued[:2]]
        assert (
            application.jobs.get_job(queued[2].job.job_id).status
            is AttemptStatus.QUEUED
        )

        factory.processes[0].finish(9)
        factory.wait_for_calls(3)
        third = factory.calls[2][0]
        assert third[third.index("--job-id") + 1] == str(queued[2].job.job_id)
    finally:
        supervisor.stop()
        for process in factory.processes:
            process.finish()
        _wait_until(lambda: supervisor.active_process_count == 0)


def test_startup_interrupts_only_stale_active_and_recovers_queued(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    stale = _queue_material(application, tmp_path, "stale-stopping")
    fresh = _queue_material(application, tmp_path, "fresh-external")
    queued = _queue_material(application, tmp_path, "restart-queued")
    assert (
        application.jobs.claim_next(
            ClaimJobCommand("old-worker", 401, stale.job.job_id)
        )
        is not None
    )
    assert (
        application.jobs.claim_next(
            ClaimJobCommand("fresh-worker", 402, fresh.job.job_id)
        )
        is not None
    )
    application.jobs.stop(StopAttemptCommand(_command_id(), stale.attempt.attempt_id))
    now = datetime.now(UTC)
    _set_heartbeat(application, stale.job.job_id, now - timedelta(hours=2))
    _set_heartbeat(application, fresh.job.job_id, now)
    factory = _RecordingProcessFactory()
    supervisor = LocalJobSupervisor(
        application,
        max_concurrency=2,
        poll_interval_sec=0.01,
        orphan_after_sec=60,
        orphan_audit_interval_sec=60,
        process_factory=factory,
    )

    supervisor.start()
    try:
        factory.wait_for_calls(1)
        assert (
            application.jobs.get_attempt(stale.attempt.attempt_id).status
            is AttemptStatus.INTERRUPTED
        )
        assert (
            application.jobs.get_attempt(fresh.attempt.attempt_id).status
            is AttemptStatus.RUNNING
        )
        launched = factory.calls[0][0]
        assert launched[launched.index("--job-id") + 1] == str(queued.job.job_id)
        assert len(factory.calls) == 1
    finally:
        supervisor.stop()
        for process in factory.processes:
            process.finish()
        _wait_until(lambda: supervisor.active_process_count == 0)


def test_shutdown_does_not_rewrite_a_detached_child_failure(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    queued = _queue_material(application, tmp_path, "shutdown")
    factory = _RecordingProcessFactory()
    supervisor = LocalJobSupervisor(
        application,
        poll_interval_sec=0.01,
        orphan_after_sec=3600,
        process_factory=factory,
    )
    supervisor.start()
    factory.wait_for_calls(1)

    supervisor.stop()
    factory.processes[0].finish(19)
    _wait_until(lambda: supervisor.active_process_count == 0)

    assert application.jobs.get_job(queued.job.job_id).status is AttemptStatus.RUNNING
    assert (
        application.jobs.get_attempt(queued.attempt.attempt_id).status
        is AttemptStatus.RUNNING
    )


def test_launch_failure_is_synchronously_persisted_as_failed(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    queued = _queue_material(application, tmp_path, "launch-failure")

    def fail_to_launch(*_args, **_kwargs):
        raise OSError("process table unavailable; api_key=super-secret-value")

    supervisor = LocalJobSupervisor(
        application,
        poll_interval_sec=0.01,
        orphan_after_sec=3600,
        process_factory=fail_to_launch,
    )
    supervisor.start()
    try:
        _wait_until(
            lambda: (
                application.jobs.get_attempt(queued.attempt.attempt_id).status
                is AttemptStatus.FAILED
            )
        )
        assert "process table unavailable" in (
            application.jobs.get_attempt(queued.attempt.attempt_id).error_message or ""
        )
        assert "super-secret-value" not in (
            application.jobs.get_attempt(queued.attempt.attempt_id).error_message or ""
        )
        assert "[REDACTED]" in (
            application.jobs.get_attempt(queued.attempt.attempt_id).error_message or ""
        )
    finally:
        supervisor.stop()


def test_one_supervisor_dispatches_all_three_durable_operation_types(
    application: CutMasterApplication,
) -> None:
    operations = ("material_analysis", "aster_planning", "rendering")
    base = datetime.now(UTC) - timedelta(minutes=1)
    expected_jobs = [
        _insert_queued_job(
            application,
            operation,
            created_at=base + timedelta(seconds=index),
        )[1]
        for index, operation in enumerate(operations)
    ]
    factory = _RecordingProcessFactory()
    supervisor = LocalJobSupervisor(
        application,
        max_concurrency=3,
        poll_interval_sec=0.01,
        orphan_after_sec=3600,
        process_factory=factory,
    )

    supervisor.start()
    try:
        factory.wait_for_calls(3)
        modules = [command[command.index("-m") + 1] for command, _ in factory.calls]
        job_ids = [
            command[command.index("--job-id") + 1] for command, _ in factory.calls
        ]
        assert modules == [
            "cutmaster.adapters.web.material_worker",
            "cutmaster.adapters.web.run_worker",
            "cutmaster.adapters.web.render_worker",
        ]
        assert job_ids == [str(job_id) for job_id in expected_jobs]
        for _command, kwargs in factory.calls:
            assert kwargs["stdin"] is not None
            assert kwargs["close_fds"] is True
            assert kwargs["start_new_session"] is True
    finally:
        supervisor.stop()
        for process in factory.processes:
            process.finish()
        _wait_until(lambda: supervisor.active_process_count == 0)


def test_fastapi_lifespan_is_the_only_supervisor_start_boundary(
    application: CutMasterApplication,
) -> None:
    class RecordingSupervisor:
        def __init__(self) -> None:
            self.started = 0
            self.stopped = 0

        def dispatch(self, _submission) -> None:
            pass

        def start(self) -> None:
            self.started += 1

        def stop(self, *, timeout_sec: float = 5.0) -> None:
            assert timeout_sec > 0
            self.stopped += 1

    supervisor = RecordingSupervisor()
    app = create_app(application=application, job_supervisor=supervisor)
    assert supervisor.started == 0
    assert app.state.cutmaster_material_dispatcher is supervisor
    assert app.state.cutmaster_run_dispatcher is supervisor
    assert app.state.cutmaster_render_dispatcher is supervisor

    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        assert supervisor.started == 1
        assert supervisor.stopped == 0

    assert supervisor.stopped == 1


def test_default_app_factory_keeps_managed_state_lazy_until_lifespan(
    application: CutMasterApplication,
) -> None:
    database = application.settings.effective_configuration.data_root / "cutmaster.db"
    assert not database.exists()

    app = create_app(application=application)

    assert not database.exists()
    supervisor = app.state.cutmaster_job_supervisor
    assert isinstance(supervisor, LocalJobSupervisor)
    assert not supervisor.is_running
