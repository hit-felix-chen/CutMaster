"""Lifecycle-aware launcher for the external Data Root migration queue."""

from __future__ import annotations

import json
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
from cutmaster.application.settings import DataRootMigrationStatus
from cutmaster.infrastructure.observability.logging import error_summary


class DataRootMigrationSupervisor(Protocol):
    def dispatch(self, migration_id: str) -> None: ...

    def start(self) -> None: ...

    def stop(self, *, timeout_sec: float = 5.0) -> None: ...


class MigrationProcess(Protocol):
    pid: int

    def poll(self) -> int | None: ...


MigrationProcessFactory = Callable[..., MigrationProcess]


class LocalDataRootMigrationSupervisor:
    def __init__(
        self,
        application: CutMasterApplication,
        *,
        poll_interval_sec: float = 0.5,
        worker_stale_after_sec: float = 45.0,
        process_factory: MigrationProcessFactory = subprocess.Popen,
    ) -> None:
        self._application = application
        self._poll_interval_sec = poll_interval_sec
        self._worker_stale_after_sec = worker_stale_after_sec
        self._process_factory = process_factory
        self._processes: dict[str, tuple[str, MigrationProcess]] = {}
        self._wake = Event()
        self._shutdown: Event | None = None
        self._thread: Thread | None = None
        self._lock = RLock()

    def dispatch(self, _migration_id: str) -> None:
        self._wake.set()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            shutdown = Event()
            thread = Thread(
                target=self._run,
                args=(shutdown,),
                name="cutmaster-data-root-migration-supervisor",
                daemon=True,
            )
            self._shutdown = shutdown
            self._thread = thread
            self._wake.set()
            thread.start()

    def stop(self, *, timeout_sec: float = 5.0) -> None:
        with self._lock:
            shutdown = self._shutdown
            thread = self._thread
        if shutdown is None or thread is None:
            return
        shutdown.set()
        self._wake.set()
        thread.join(timeout_sec)
        with self._lock:
            if not thread.is_alive():
                self._shutdown = None
                self._thread = None

    def _run(self, shutdown: Event) -> None:
        while not shutdown.is_set():
            self._reap_processes()
            migration = self._application.settings.migrations.current()
            if migration is not None and self._should_launch(migration):
                self._launch(migration.migration_id)
            self._wake.wait(self._poll_interval_sec)
            self._wake.clear()

    def _should_launch(self, migration: object) -> bool:
        migration_id = str(getattr(migration, "migration_id"))
        with self._lock:
            tracked = self._processes.get(migration_id)
        if tracked is not None and tracked[1].poll() is None:
            return False
        status = getattr(migration, "status")
        if status in {
            DataRootMigrationStatus.CANCELLED,
            DataRootMigrationStatus.FAILED,
            DataRootMigrationStatus.SWITCHING,
            DataRootMigrationStatus.RESTART_REQUIRED,
            DataRootMigrationStatus.COMPLETE,
        }:
            return False
        heartbeat = getattr(migration, "heartbeat_at")
        if heartbeat is None:
            return True
        return heartbeat < datetime.now(UTC) - timedelta(
            seconds=self._worker_stale_after_sec
        )

    def _launch(self, migration_id: str) -> None:
        configuration = self._application.settings.effective_configuration
        worker_id = f"data-root-launch-{os.getpid()}-{uuid4()}"
        if not self._application.settings.migrations.store.reserve_worker_launch(
            migration_id,
            worker_id=worker_id,
            stale_after_sec=self._worker_stale_after_sec,
        ):
            return
        log_path = (
            self._application.data_root_coordinator.control_root
            / "migrations"
            / migration_id
            / "worker.log"
        )
        log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        command = (
            sys.executable,
            "-m",
            "cutmaster.adapters.web.data_root_migration_worker",
            "--config",
            str(configuration.sources.base_path),
            "--migration-id",
            migration_id,
            "--reserved-worker-id",
            worker_id,
        )
        try:
            with log_path.open("ab", buffering=0) as stream:
                process = self._process_factory(
                    command,
                    cwd=configuration.sources.base_path.parent,
                    stdin=subprocess.DEVNULL,
                    stdout=stream,
                    stderr=subprocess.STDOUT,
                    close_fds=True,
                    start_new_session=True,
                )
            self._application.settings.migrations.store.adopt_reserved_worker(
                migration_id,
                worker_id=worker_id,
                process_id=process.pid,
            )
            with self._lock:
                self._processes[migration_id] = (worker_id, process)
        except OSError as error:
            self._application.settings.migrations.store.update(
                migration_id,
                status=DataRootMigrationStatus.FAILED,
                phase="failed",
                failure_code="worker_launch_failed",
                failure_message=error_summary(error) or type(error).__name__,
                terminal=True,
            )
            self._application.data_root_coordinator.set_state(
                maintenance=False,
                restart_required=False,
                migration_id=migration_id,
                migration_status=DataRootMigrationStatus.FAILED,
            )

    def _reap_processes(self) -> None:
        with self._lock:
            snapshot = tuple(self._processes.items())
        for migration_id, (worker_id, process) in snapshot:
            return_code = process.poll()
            if return_code is None:
                continue
            with self._lock:
                current = self._processes.get(migration_id)
                if (
                    current is None
                    or current[0] != worker_id
                    or current[1] is not process
                ):
                    continue
                self._processes.pop(migration_id, None)
            migration = self._application.settings.migrations.get(migration_id)
            if return_code != 0 and migration.status not in {
                DataRootMigrationStatus.CANCELLED,
                DataRootMigrationStatus.FAILED,
                DataRootMigrationStatus.SWITCHING,
                DataRootMigrationStatus.RESTART_REQUIRED,
                DataRootMigrationStatus.COMPLETE,
            }:
                self._application.settings.migrations.store.update(
                    migration_id,
                    status=DataRootMigrationStatus.FAILED,
                    phase="failed",
                    failure_code="worker_process_failed",
                    failure_message=(
                        f"Data Root migration worker exited with status {return_code}"
                    ),
                    terminal=True,
                )
                self._application.data_root_coordinator.set_state(
                    maintenance=False,
                    restart_required=False,
                    migration_id=migration_id,
                    migration_status=DataRootMigrationStatus.FAILED,
                )
                continue
            self._application.settings.migrations.store.release_worker(
                migration_id,
                worker_id=worker_id,
                process_id=process.pid,
            )
            self._wake.set()


def finalize_migration_after_restart(application: CutMasterApplication) -> bool:
    """Reconcile a migration process crash before normal supervisors start."""

    coordinator = application.data_root_coordinator
    state = coordinator.state()
    if state.migration_id is None or not (state.maintenance or state.restart_required):
        return False
    migration = application.settings.migrations.get(state.migration_id)
    current_root = application.settings.effective_configuration.data_root.resolve()
    source_root = migration.source_root.resolve()
    destination_root = migration.destination_root.resolve()

    if migration.status in {
        DataRootMigrationStatus.CANCELLED,
        DataRootMigrationStatus.FAILED,
    }:
        if current_root != source_root:
            raise RuntimeError("A failed migration no longer points at its source root")
        coordinator.set_state(
            maintenance=False,
            restart_required=False,
            migration_id=migration.migration_id,
            migration_status=migration.status,
        )
        return False

    if migration.status is DataRootMigrationStatus.SWITCHING and current_root == source_root:
        application.settings.migrations.store.recover_switch_before_commit(
            migration.migration_id
        )
        coordinator.set_state(
            maintenance=True,
            restart_required=False,
            migration_id=migration.migration_id,
            migration_status=DataRootMigrationStatus.VERIFYING,
        )
        return False

    finalizable = migration.status in {
        DataRootMigrationStatus.SWITCHING,
        DataRootMigrationStatus.RESTART_REQUIRED,
        DataRootMigrationStatus.COMPLETE,
    }
    if not finalizable or current_root != destination_root:
        return False
    _validate_destination_marker(migration.destination_root, migration.migration_id)
    if migration.status is not DataRootMigrationStatus.COMPLETE:
        application.settings.migrations.store.update(
            migration.migration_id,
            status=DataRootMigrationStatus.COMPLETE,
            phase="complete",
            terminal=True,
        )
    coordinator.set_state(
        maintenance=False,
        restart_required=False,
        migration_id=migration.migration_id,
        migration_status=DataRootMigrationStatus.COMPLETE,
    )
    return True


def _validate_destination_marker(destination: Path, migration_id: str) -> None:
    marker = destination / ".cutmaster-root-owner.json"
    if marker.is_symlink() or not marker.is_file():
        raise RuntimeError("Migrated Data Root ownership marker is invalid")
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError) as error:
        raise RuntimeError("Migrated Data Root ownership marker is invalid") from error
    if value.get("schema_version") != "1.0" or value.get("migration_id") != migration_id:
        raise RuntimeError("Migrated Data Root ownership marker is invalid")


__all__ = [
    "DataRootMigrationSupervisor",
    "LocalDataRootMigrationSupervisor",
    "finalize_migration_after_restart",
]
