from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from cutmaster.adapters.web import create_app
from cutmaster.adapters.web.data_root_migration_supervisor import (
    LocalDataRootMigrationSupervisor,
)
from cutmaster.adapters.web.data_root_migration_worker import (
    execute_data_root_migration,
)
from cutmaster.application import CutMasterApplication
from cutmaster.application.projects import CreateProjectCommand


class RecordingMigrationSupervisor:
    def __init__(self) -> None:
        self.dispatched: list[str] = []
        self.started = False

    def dispatch(self, migration_id: str) -> None:
        self.dispatched.append(migration_id)

    def start(self) -> None:
        self.started = True

    def stop(self, *, timeout_sec: float = 5.0) -> None:
        del timeout_sec
        self.started = False


def _key(value: str | None = None) -> dict[str, str]:
    return {"Idempotency-Key": value or str(uuid4())}


def test_migration_http_contract_is_durable_and_replays_after_copy(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    application.projects.create(CreateProjectCommand(str(uuid4()), "HTTP migration"))
    supervisor = RecordingMigrationSupervisor()
    web = create_app(
        application=application,
        data_root_migration_supervisor=supervisor,
    )
    destination = tmp_path / "http-destination"
    command_id = str(uuid4())

    with TestClient(web) as client:
        preflight = client.post(
            "/api/settings/storage/migrations/preflight",
            json={"destination_root": str(destination)},
        )
        started = client.post(
            "/api/settings/storage/migrations",
            headers=_key(command_id),
            json={"destination_root": str(destination)},
        )

        assert preflight.status_code == 200
        assert preflight.json()["eligible"] is True
        assert started.status_code == 202
        body = started.json()
        migration_id = body["migration_id"]
        assert body["status"] == "requested"
        assert body["progress"]["phase"] == "admission"
        assert body["failure"] is None
        assert started.headers["location"].endswith(migration_id)
        assert started.headers["retry-after"] == "1"
        assert supervisor.dispatched == [migration_id]

        assert execute_data_root_migration(
            application,
            migration_id,
            worker_id="http-test-worker",
            process_id=1234,
        )
        replay = client.post(
            "/api/settings/storage/migrations",
            headers=_key(command_id),
            json={"destination_root": str(destination)},
        )
        current = client.get("/api/settings/storage/migrations/current")
        health = client.get("/api/health")

        assert replay.status_code == 202
        assert replay.json()["migration_id"] == migration_id
        assert replay.json()["status"] == "restart_required"
        assert current.json()["migration"]["migration_id"] == migration_id
        assert health.json()["status"] == "restart_required"


def test_migration_preflight_and_problem_keep_typed_blockers(
    application: CutMasterApplication,
) -> None:
    supervisor = RecordingMigrationSupervisor()
    web = create_app(
        application=application,
        data_root_migration_supervisor=supervisor,
    )
    source = application.settings.effective_configuration.data_root

    with TestClient(web) as client:
        preflight = client.post(
            "/api/settings/storage/migrations/preflight",
            json={"destination_root": str(source)},
        )
        rejected = client.post(
            "/api/settings/storage/migrations",
            headers=_key(),
            json={"destination_root": str(source)},
        )

    assert preflight.status_code == 200
    assert preflight.json()["eligible"] is False
    assert {item["kind"] for item in preflight.json()["blockers"]} >= {"same_root"}
    assert all(
        set(item) == {"kind", "metadata"}
        for item in preflight.json()["blockers"]
    )
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "data_root_migration_blocked"
    assert {item["kind"] for item in rejected.json()["blockers"]} >= {"same_root"}
    assert all(
        set(item) == {"kind", "metadata"}
        for item in rejected.json()["blockers"]
    )


def test_cancel_endpoint_is_idempotent_and_worker_rolls_back_to_cancelled(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    supervisor = RecordingMigrationSupervisor()
    web = create_app(
        application=application,
        data_root_migration_supervisor=supervisor,
    )
    destination = tmp_path / "cancel-endpoint-destination"
    cancel_id = str(uuid4())

    with TestClient(web) as client:
        started = client.post(
            "/api/settings/storage/migrations",
            headers=_key(),
            json={"destination_root": str(destination)},
        )
        migration_id = started.json()["migration_id"]
        cancelled = client.post(
            f"/api/settings/storage/migrations/{migration_id}/cancel",
            headers=_key(cancel_id),
        )
        replay = client.post(
            f"/api/settings/storage/migrations/{migration_id}/cancel",
            headers=_key(cancel_id),
        )
        detail = client.get(f"/api/settings/storage/migrations/{migration_id}")

        assert cancelled.status_code == replay.status_code == 202
        assert cancelled.json()["status"] == "cancelling"
        assert cancelled.json()["cancel_requested"] is True
        assert replay.json()["migration_id"] == migration_id
        assert detail.status_code == 200

        assert execute_data_root_migration(
            application,
            migration_id,
            worker_id="cancel-test-worker",
            process_id=1234,
        )
        terminal_replay = client.post(
            f"/api/settings/storage/migrations/{migration_id}/cancel",
            headers=_key(cancel_id),
        )

    assert terminal_replay.status_code == 202
    assert terminal_replay.json()["status"] == "cancelled"
    assert application.data_root_coordinator.state().maintenance is False


def test_maintenance_and_restart_block_business_reads_before_handler(
    application: CutMasterApplication,
) -> None:
    supervisor = RecordingMigrationSupervisor()
    web = create_app(
        application=application,
        data_root_migration_supervisor=supervisor,
    )
    entered: list[bool] = []

    @web.get("/api/maintenance-probe")
    def probe() -> dict[str, bool]:
        entered.append(True)
        return {"entered": True}

    migration = application.settings.migrations.start(
        command_id=str(uuid4()),
        destination_root=(
            application.settings.effective_configuration.data_root.parent
            / "maintenance-destination"
        ),
    )
    with TestClient(web) as client:
        blocked = client.get("/api/maintenance-probe")
        health = client.get("/api/health")
        migration_response = client.get("/api/settings/storage/migrations/current")

        assert blocked.status_code == 503
        assert blocked.json()["code"] == "data_root_maintenance"
        assert blocked.headers["retry-after"] == "5"
        assert entered == []
        assert health.status_code == 200
        assert migration_response.status_code == 200

        application.data_root_coordinator.set_state(
            maintenance=True,
            restart_required=True,
            migration_id=migration.migration_id,
            migration_status="restart_required",
        )
        restart_blocked = client.get("/api/maintenance-probe")

        assert restart_blocked.status_code == 503
        assert restart_blocked.json()["code"] == "data_root_restart_required"
        assert entered == []


def test_migration_failure_redacts_internal_error_and_only_projects_stable_code(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    migration = application.settings.migrations.start(
        command_id=str(uuid4()),
        destination_root=tmp_path / "launch-error-destination",
    )

    def fail_to_launch(*_args, **_kwargs):
        raise OSError("api_key=super-secret-migration-token")

    supervisor = LocalDataRootMigrationSupervisor(
        application,
        process_factory=fail_to_launch,
    )
    supervisor._launch(migration.migration_id)
    stored = application.settings.migrations.get(migration.migration_id)

    assert stored.failure_message is not None
    assert "super-secret-migration-token" not in stored.failure_message
    assert "[REDACTED]" in stored.failure_message

    web = create_app(
        application=application,
        data_root_migration_supervisor=RecordingMigrationSupervisor(),
    )
    with TestClient(web) as client:
        response = client.get(
            f"/api/settings/storage/migrations/{migration.migration_id}"
        )

    assert response.status_code == 200
    assert response.json()["failure"] == {"code": "worker_launch_failed"}
    assert "super-secret-migration-token" not in response.text
