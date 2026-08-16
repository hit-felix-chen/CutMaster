"""Application-level Data Root Migration regression tests."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import pytest

from cutmaster import CutMasterApplication
from cutmaster.adapters.web.data_root_migration_supervisor import (
    LocalDataRootMigrationSupervisor,
    finalize_migration_after_restart,
)
from cutmaster.adapters.web.data_root_migration_worker import (
    execute_data_root_migration,
)
from cutmaster.application.ports.data_root import DataRootRestartRequiredError
from cutmaster.application.projects import CreateProjectCommand
from cutmaster.application.settings import DataRootMigrationStatus
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.domain.ids import RunId
from cutmaster.infrastructure.storage.local.data_root_migration import (
    LocalDataRootMigrator,
)


@pytest.fixture
def config_path(managed_configuration: EffectiveConfiguration) -> Path:
    return managed_configuration.sources.base_path


@pytest.fixture
def application(config_path: Path) -> CutMasterApplication:
    return CutMasterApplication.open(config_path)


def _start_and_execute(
    application: CutMasterApplication,
    destination: Path,
) -> str:
    migration = application.settings.migrations.start(
        command_id=str(uuid4()),
        destination_root=destination,
    )
    assert execute_data_root_migration(
        application,
        migration.migration_id,
        worker_id=f"test-{uuid4()}",
        process_id=1234,
    )
    completed = application.settings.migrations.get(migration.migration_id)
    assert completed.status is DataRootMigrationStatus.RESTART_REQUIRED
    return migration.migration_id


def test_verified_migration_can_be_repeated_from_an_owned_destination(
    application: CutMasterApplication,
    config_path: Path,
    tmp_path: Path,
) -> None:
    application.projects.create(
        CreateProjectCommand(str(uuid4()), "Survives two migrations")
    )
    first_destination = tmp_path / "first-root"
    _start_and_execute(application, first_destination)

    second_application = CutMasterApplication.open(config_path)
    assert finalize_migration_after_restart(second_application)
    second_destination = tmp_path / "second-root"
    _start_and_execute(second_application, second_destination)

    third_application = CutMasterApplication.open(config_path)
    assert finalize_migration_after_restart(third_application)
    assert [item.name for item in third_application.projects.list()] == [
        "Survives two migrations"
    ]
    assert (first_destination / ".cutmaster-root-owner.json").is_file()
    assert (second_destination / ".cutmaster-root-owner.json").is_file()


def test_control_directory_overlap_is_blocked_in_both_directions(
    application: CutMasterApplication,
) -> None:
    control_root = application.data_root_coordinator.control_root

    child = application.settings.migrations.preflight(control_root / "destination")
    parent = application.settings.migrations.preflight(control_root.parent)

    assert "control_root_overlap" in {item.kind for item in child.blockers}
    assert "control_root_overlap" in {item.kind for item in parent.blockers}


def test_unmanifested_destination_entry_prevents_switch_and_is_not_deleted(
    application: CutMasterApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application.projects.create(CreateProjectCommand(str(uuid4()), "Source"))
    destination = tmp_path / "destination"
    migration = application.settings.migrations.start(
        command_id=str(uuid4()),
        destination_root=destination,
    )
    migrator = LocalDataRootMigrator(
        application.settings.effective_configuration,
        application.data_root_coordinator,
        application.settings.migrations.store,
    )
    original_verify = migrator._verify_destination

    def inject_unowned_file(*args, **kwargs) -> None:
        original_verify(*args, **kwargs)
        (destination / "user-sentinel.txt").write_text("do not delete", encoding="utf-8")

    monkeypatch.setattr(migrator, "_verify_destination", inject_unowned_file)

    result = migrator.execute(migration.migration_id)

    assert result.status is DataRootMigrationStatus.FAILED
    assert {item.kind for item in result.blockers} >= {
        "unmanifested_destination_entry",
        "cleanup_incomplete",
    }
    assert (destination / "user-sentinel.txt").read_text(encoding="utf-8") == (
        "do not delete"
    )
    assert not application.settings.effective_configuration.sources.data_root_pointer_path.exists()


def test_cancel_rolls_back_only_manifest_owned_files_and_preserves_new_sentinel(
    application: CutMasterApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application.projects.create(CreateProjectCommand(str(uuid4()), "Source"))
    destination = tmp_path / "cancelled-destination"
    migration = application.settings.migrations.start(
        command_id=str(uuid4()),
        destination_root=destination,
    )
    migrator = LocalDataRootMigrator(
        application.settings.effective_configuration,
        application.data_root_coordinator,
        application.settings.migrations.store,
    )

    def cancel_after_verification(_migration, _manifest) -> None:
        (destination / "user-sentinel.txt").write_text("keep", encoding="utf-8")
        application.settings.migrations.cancel(
            command_id=str(uuid4()),
            migration_id=migration.migration_id,
        )

    monkeypatch.setattr(
        migrator,
        "_verify_no_unmanifested_entries",
        cancel_after_verification,
    )

    result = migrator.execute(migration.migration_id)

    assert result.status is DataRootMigrationStatus.CANCELLED
    assert {item.kind for item in result.blockers} == {"cleanup_incomplete"}
    assert (destination / "user-sentinel.txt").read_text(encoding="utf-8") == "keep"
    assert not (destination / ".cutmaster-root-owner.json").exists()
    assert not (destination / "cutmaster.db").exists()


def test_pointer_switch_invalidates_every_old_persistent_read_surface(
    application: CutMasterApplication,
    config_path: Path,
    tmp_path: Path,
) -> None:
    application.projects.create(CreateProjectCommand(str(uuid4()), "Old generation"))
    # Materialise each lazy service before the pointer changes.
    application.materials.list()
    application.projects.list()
    application.settings.storage_report()
    _ = application.runs

    _start_and_execute(application, tmp_path / "new-root")
    replacement = CutMasterApplication.open(config_path)
    assert finalize_migration_after_restart(replacement)

    calls = (
        application.materials.list,
        application.projects.list,
        lambda: application.runs.get(RunId.new()),
        application.settings.storage_report,
    )
    for call in calls:
        with pytest.raises(DataRootRestartRequiredError):
            call()


def test_stale_heartbeat_does_not_steal_a_live_migration_process(
    application: CutMasterApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = application.settings.migrations.start(
        command_id=str(uuid4()),
        destination_root=tmp_path / "live-worker-destination",
    )
    store = application.settings.migrations.store
    assert store.claim_worker(
        migration.migration_id,
        worker_id="live-worker",
        process_id=os.getpid(),
    )
    with store._connect() as connection:
        connection.execute(
            "UPDATE data_root_migrations SET heartbeat_at = ? WHERE migration_id = ?",
            ("1970-01-01T00:00:00+00:00", migration.migration_id),
        )
        connection.commit()

    assert not store.reserve_worker_launch(
        migration.migration_id,
        worker_id="replacement",
        stale_after_sec=0.01,
    )
    monkeypatch.setattr(
        "cutmaster.application.settings.data_root_migration._process_is_alive",
        lambda _process_id: False,
    )
    assert store.reserve_worker_launch(
        migration.migration_id,
        worker_id="replacement",
        stale_after_sec=0.01,
    )


def test_supervisors_atomically_reserve_one_slow_worker_launch(
    application: CutMasterApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = application.settings.migrations.start(
        command_id=str(uuid4()),
        destination_root=tmp_path / "slow-launch-destination",
    )
    commands: list[tuple[str, ...]] = []

    class SlowProcess:
        pid = 987_654

        def poll(self) -> None:
            return None

    def launch(command, **_kwargs):
        commands.append(tuple(command))
        return SlowProcess()

    first = LocalDataRootMigrationSupervisor(application, process_factory=launch)
    second = LocalDataRootMigrationSupervisor(application, process_factory=launch)

    first._launch(migration.migration_id)
    reserved = application.settings.migrations.get(migration.migration_id)
    assert reserved.process_id == SlowProcess.pid
    with application.settings.migrations.store._connect() as connection:
        connection.execute(
            "UPDATE data_root_migrations SET heartbeat_at = ? WHERE migration_id = ?",
            ("1970-01-01T00:00:00+00:00", migration.migration_id),
        )
        connection.commit()
    monkeypatch.setattr(
        "cutmaster.application.settings.data_root_migration._process_is_alive",
        lambda process_id: process_id == SlowProcess.pid,
    )
    second._launch(migration.migration_id)
    current = application.settings.migrations.get(migration.migration_id)

    assert len(commands) == 1
    assert "--reserved-worker-id" in commands[0]
    assert first._should_launch(current) is False


def test_nonzero_migration_worker_exit_fails_once_without_fork_storm(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    migration = application.settings.migrations.start(
        command_id=str(uuid4()),
        destination_root=tmp_path / "fatal-worker-destination",
    )
    commands: list[tuple[str, ...]] = []

    class FailedProcess:
        pid = 876_543

        def poll(self) -> int:
            return 19

    def launch(command, **_kwargs):
        commands.append(tuple(command))
        return FailedProcess()

    supervisor = LocalDataRootMigrationSupervisor(application, process_factory=launch)
    supervisor._launch(migration.migration_id)
    supervisor._reap_processes()
    supervisor._launch(migration.migration_id)

    failed = application.settings.migrations.get(migration.migration_id)
    assert len(commands) == 1
    assert failed.status is DataRootMigrationStatus.FAILED
    assert failed.failure_code == "worker_process_failed"
    assert application.data_root_coordinator.state().maintenance is False


def test_startup_requeues_switching_record_when_pointer_was_not_committed(
    application: CutMasterApplication,
    config_path: Path,
    tmp_path: Path,
) -> None:
    migration = application.settings.migrations.start(
        command_id=str(uuid4()),
        destination_root=tmp_path / "not-yet-switched-destination",
    )
    application.settings.migrations.store.update(
        migration.migration_id,
        status=DataRootMigrationStatus.SWITCHING,
        phase="switching_pointer",
    )
    application.data_root_coordinator.set_state(
        maintenance=True,
        restart_required=True,
        migration_id=migration.migration_id,
        migration_status=DataRootMigrationStatus.SWITCHING,
    )

    reopened = CutMasterApplication.open(config_path)
    assert finalize_migration_after_restart(reopened) is False

    recovered = reopened.settings.migrations.get(migration.migration_id)
    state = reopened.data_root_coordinator.state()
    assert recovered.status is DataRootMigrationStatus.VERIFYING
    assert recovered.phase == "recovering_before_switch"
    assert recovered.worker_id is None
    assert state.maintenance is True
    assert state.restart_required is False


def test_startup_completes_switching_record_when_pointer_was_committed(
    application: CutMasterApplication,
    config_path: Path,
    tmp_path: Path,
) -> None:
    migration_id = _start_and_execute(application, tmp_path / "switched-destination")
    application.settings.migrations.store.update(
        migration_id,
        status=DataRootMigrationStatus.SWITCHING,
        phase="switching_pointer",
    )

    reopened = CutMasterApplication.open(config_path)
    assert finalize_migration_after_restart(reopened) is True
    assert (
        reopened.settings.migrations.get(migration_id).status
        is DataRootMigrationStatus.COMPLETE
    )
    assert reopened.data_root_coordinator.state().maintenance is False
