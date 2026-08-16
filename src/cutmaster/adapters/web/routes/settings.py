"""Secret-free Effective Configuration and storage routes."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response, status

from cutmaster.adapters.web.dependencies import (
    ApplicationDependency,
    IdempotencyKey,
)
from cutmaster.adapters.web.presenters import (
    data_root_migration_preflight_view,
    data_root_migration_view,
    settings_view,
    storage_report_view,
)
from cutmaster.adapters.web.schemas.settings import (
    ASRConnectionTestBody,
    DataRootMigrationBody,
    ModelConnectionTestBody,
    SaveProviderSettingsBody,
    SaveSettingsBody,
)
from cutmaster.application.settings import (
    CredentialUpdate,
    ProbeProviderConnectionCommand,
    SaveProviderSettingsCommand,
    SaveSettingsCommand,
)

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
def get_settings(application: ApplicationDependency) -> dict[str, object]:
    return settings_view(application.settings.get())


@router.put("")
def save_settings(
    body: SaveSettingsBody,
    application: ApplicationDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    result = application.settings.save(SaveSettingsCommand(command_id, body.overlay))
    return {
        "settings": settings_view(result.settings),
        "restart_required": result.restart_required,
    }


@router.put("/providers")
def save_provider_settings(
    body: SaveProviderSettingsBody,
    application: ApplicationDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    providers = (
        None if body.providers is None else body.providers.model_dump(mode="python")
    )
    credentials = {
        capability: CredentialUpdate(
            update.action,
            None if update.value is None else update.value.get_secret_value(),
        )
        for capability, update in (
            ("llm", body.credentials.llm),
            ("vlm", body.credentials.vlm),
            ("asr", body.credentials.asr),
        )
    }
    result = application.settings.save_providers(
        SaveProviderSettingsCommand(
            command_id,
            body.profile,
            providers,
            credentials,
        )
    )
    return {
        "settings": settings_view(result.settings),
        "restart_required": result.restart_required,
        "credential_results": dict(result.credential_results),
    }


def _test_connection(
    capability: str,
    body: ModelConnectionTestBody | ASRConnectionTestBody,
    application: ApplicationDependency,
) -> dict[str, object]:
    api_key = None if body.api_key is None else body.api_key.get_secret_value()
    result = application.settings.test_provider(
        ProbeProviderConnectionCommand(
            capability,  # type: ignore[arg-type]
            body.configuration.model_dump(mode="python"),
            api_key,
        )
    )
    return {
        "capability": result.capability,
        "status": result.status,
        "latency_ms": result.latency_ms,
    }


@router.post("/providers/llm/test")
def test_llm(
    body: ModelConnectionTestBody,
    application: ApplicationDependency,
) -> dict[str, object]:
    return _test_connection("llm", body, application)


@router.post("/providers/vlm/test")
def test_vlm(
    body: ModelConnectionTestBody,
    application: ApplicationDependency,
) -> dict[str, object]:
    return _test_connection("vlm", body, application)


@router.post("/providers/asr/test")
def test_asr(
    body: ASRConnectionTestBody,
    application: ApplicationDependency,
) -> dict[str, object]:
    return _test_connection("asr", body, application)


@router.get("/storage")
def storage(application: ApplicationDependency) -> dict[str, object]:
    return storage_report_view(application.settings.storage_report())


@router.post("/storage/reveal")
def reveal_storage(
    application: ApplicationDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    result = application.settings.reveal_data_root(command_id)
    return {"opened": result.opened}


@router.post("/storage/migrations/preflight")
def preflight_data_root_migration(
    body: DataRootMigrationBody,
    application: ApplicationDependency,
) -> dict[str, object]:
    return data_root_migration_preflight_view(
        application.settings.migrations.preflight(body.destination_root)
    )


@router.post(
    "/storage/migrations",
    status_code=status.HTTP_202_ACCEPTED,
)
def start_data_root_migration(
    body: DataRootMigrationBody,
    application: ApplicationDependency,
    command_id: IdempotencyKey,
    request: Request,
    response: Response,
) -> dict[str, object]:
    migration = application.settings.migrations.start(
        command_id=command_id,
        destination_root=body.destination_root,
    )
    supervisor = getattr(
        request.app.state,
        "cutmaster_data_root_migration_supervisor",
        None,
    )
    if supervisor is None:
        raise RuntimeError("Data Root Migration Supervisor is unavailable")
    supervisor.dispatch(migration.migration_id)
    response.headers["Location"] = (
        f"/api/settings/storage/migrations/{migration.migration_id}"
    )
    response.headers["Retry-After"] = "1"
    return data_root_migration_view(migration)


@router.get("/storage/migrations/current")
def current_data_root_migration(
    application: ApplicationDependency,
) -> dict[str, object | None]:
    migration = application.settings.migrations.current()
    return {
        "migration": (
            None if migration is None else data_root_migration_view(migration)
        )
    }


@router.get("/storage/migrations/{migration_id}")
def get_data_root_migration(
    migration_id: str,
    application: ApplicationDependency,
) -> dict[str, object]:
    return data_root_migration_view(application.settings.migrations.get(migration_id))


@router.post(
    "/storage/migrations/{migration_id}/cancel",
    status_code=status.HTTP_202_ACCEPTED,
)
def cancel_data_root_migration(
    migration_id: str,
    application: ApplicationDependency,
    command_id: IdempotencyKey,
    request: Request,
    response: Response,
) -> dict[str, object]:
    migration = application.settings.migrations.cancel(
        command_id=command_id,
        migration_id=migration_id,
    )
    supervisor = getattr(
        request.app.state,
        "cutmaster_data_root_migration_supervisor",
        None,
    )
    if supervisor is not None:
        supervisor.dispatch(migration.migration_id)
    response.headers["Retry-After"] = "1"
    return data_root_migration_view(migration)


__all__ = ["router"]
