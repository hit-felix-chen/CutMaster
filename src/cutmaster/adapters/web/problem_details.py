"""RFC Problem Details mapping for the local Web adapter."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from cutmaster.adapters.web.presenters import (
    data_root_migration_blockers,
    material_reference_views,
)
from cutmaster.application.errors import (
    ApplicationError,
    MaterialMemoryTabNotFoundError,
    MaterialMemoryUnavailableError,
    MaterialPreviewUnavailableError,
    ProjectCoverUnavailableError,
    ProviderConnectionFailedError,
    RenderDispatchFailedError,
    StorageRevealFailedError,
    StorageRevealUnavailableError,
)
from cutmaster.application.ports.data_root import (
    DataRootMaintenanceError,
    DataRootRestartRequiredError,
)
from cutmaster.application.settings import (
    DataRootMigrationBlockedError,
    DataRootMigrationCancellationTooLateError,
    DataRootMigrationConflictError,
    DataRootMigrationIdempotencyError,
    DataRootMigrationNotFoundError,
)
from cutmaster.infrastructure.persistence.sqlite import (
    ActiveAttemptBlocker,
    ManagedStateConflict,
    ManagedStateNotFound,
)
from cutmaster.infrastructure.storage.local.material_catalog import (
    MaterialConsumedError,
    MaterialInconsistentError,
    MaterialNameCollisionError,
    MaterialNotFoundError,
    MaterialReferencedError,
)

LOGGER = logging.getLogger(__name__)
PROBLEM_MEDIA_TYPE = "application/problem+json"


def problem_response(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    command_id: str | None = None,
    field_errors: list[dict[str, Any]] | None = None,
    blockers: list[dict[str, Any]] | None = None,
    retryable: bool = False,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "type": f"urn:cutmaster:problem:{code}",
        "title": title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
        "code": code,
        "retryable": retryable,
    }
    if command_id is not None:
        payload["command_id"] = command_id
    if field_errors:
        payload["field_errors"] = field_errors
    if blockers:
        payload["blockers"] = blockers
    return JSONResponse(
        status_code=status,
        content=payload,
        media_type=PROBLEM_MEDIA_TYPE,
    )


def _command_id(request: Request) -> str | None:
    return request.headers.get("Idempotency-Key")


def install_problem_handlers(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def request_validation(
        request: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        field_errors = [
            {
                "field": ".".join(str(item) for item in issue["loc"]),
                "message": issue["msg"],
                "type": issue["type"],
            }
            for issue in error.errors()
        ]
        return problem_response(
            request,
            status=422,
            code="request_validation_failed",
            title="Request validation failed",
            detail="One or more request fields are invalid.",
            command_id=_command_id(request),
            field_errors=field_errors,
        )

    @app.exception_handler(HTTPException)
    async def http_exception(request: Request, error: HTTPException) -> JSONResponse:
        status = int(error.status_code)
        code = "resource_not_found" if status == 404 else "http_error"
        return problem_response(
            request,
            status=status,
            code=code,
            title="Resource not found" if status == 404 else "HTTP error",
            detail=str(error.detail),
            command_id=_command_id(request),
        )

    @app.exception_handler(ManagedStateNotFound)
    async def managed_not_found(
        request: Request,
        error: ManagedStateNotFound,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=404,
            code="resource_not_found",
            title="Resource not found",
            detail=str(error),
            command_id=_command_id(request),
        )

    @app.exception_handler(MaterialNotFoundError)
    async def material_not_found(
        request: Request,
        error: MaterialNotFoundError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=404,
            code="material_not_found",
            title="Material not found",
            detail=str(error),
            command_id=_command_id(request),
        )

    @app.exception_handler(MaterialMemoryTabNotFoundError)
    async def memory_tab_not_found(
        request: Request,
        error: MaterialMemoryTabNotFoundError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=404,
            code=error.code,
            title="Material Memory tab not found",
            detail=str(error),
        )

    @app.exception_handler(MaterialMemoryUnavailableError)
    async def memory_unavailable(
        request: Request,
        error: MaterialMemoryUnavailableError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=409,
            code=error.code,
            title="Material Memory unavailable",
            detail=str(error),
            retryable=False,
        )

    @app.exception_handler(MaterialPreviewUnavailableError)
    async def material_preview_unavailable(
        request: Request,
        error: MaterialPreviewUnavailableError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=404,
            code=error.code,
            title="Material preview unavailable",
            detail=str(error),
            retryable=False,
        )

    @app.exception_handler(ProjectCoverUnavailableError)
    async def project_cover_unavailable(
        request: Request,
        error: ProjectCoverUnavailableError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=404,
            code=error.code,
            title="Project cover unavailable",
            detail=str(error),
            retryable=False,
        )

    @app.exception_handler(MaterialNameCollisionError)
    async def material_name_collision(
        request: Request,
        error: MaterialNameCollisionError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=409,
            code="material_name_collision",
            title="Material Name already exists",
            detail=str(error),
            command_id=_command_id(request),
        )

    @app.exception_handler(MaterialInconsistentError)
    async def material_inconsistent(
        request: Request,
        error: MaterialInconsistentError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=409,
            code="material_inconsistent",
            title="Material is inconsistent",
            detail=str(error),
            command_id=_command_id(request),
        )

    @app.exception_handler(MaterialReferencedError)
    async def material_referenced(
        request: Request,
        error: MaterialReferencedError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=409,
            code="material_referenced",
            title="Material is still referenced",
            detail=str(error),
            command_id=_command_id(request),
            blockers=material_reference_views(
                request.app.state.cutmaster_application,
                error.references,
            ),
        )

    @app.exception_handler(MaterialConsumedError)
    async def material_consumed(
        request: Request,
        error: MaterialConsumedError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=409,
            code="material_has_active_consumers",
            title="Material is in use",
            detail=str(error),
            command_id=_command_id(request),
            blockers=[{"kind": "active_consumer"}],
        )

    @app.exception_handler(ActiveAttemptBlocker)
    async def active_attempts(
        request: Request,
        error: ActiveAttemptBlocker,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=409,
            code=error.code,
            title="Active work blocks this command",
            detail=str(error),
            command_id=_command_id(request),
            blockers=[
                {"type": "attempt", "attempt_id": value} for value in error.attempt_ids
            ],
        )

    @app.exception_handler(ManagedStateConflict)
    async def managed_conflict(
        request: Request,
        error: ManagedStateConflict,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=409,
            code=error.code,
            title="State conflict",
            detail=str(error),
            command_id=_command_id(request),
        )

    @app.exception_handler(RenderDispatchFailedError)
    async def render_dispatch_failed(
        request: Request,
        error: RenderDispatchFailedError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=500,
            code=error.code,
            title="Renderer worker launch failed",
            detail=str(error),
            command_id=_command_id(request),
            retryable=True,
        )

    @app.exception_handler(ProviderConnectionFailedError)
    async def provider_connection_failed(
        request: Request,
        error: ProviderConnectionFailedError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=502,
            code=error.code,
            title="Provider connection test failed",
            detail=str(error),
            retryable=True,
        )

    @app.exception_handler(StorageRevealUnavailableError)
    async def storage_reveal_unavailable(
        request: Request,
        error: StorageRevealUnavailableError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=501,
            code=error.code,
            title="File-manager reveal unavailable",
            detail=str(error),
            retryable=False,
        )

    @app.exception_handler(StorageRevealFailedError)
    async def storage_reveal_failed(
        request: Request,
        error: StorageRevealFailedError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=500,
            code=error.code,
            title="File-manager reveal failed",
            detail=str(error),
            command_id=_command_id(request),
            retryable=True,
        )

    @app.exception_handler(DataRootMigrationBlockedError)
    async def data_root_migration_blocked(
        request: Request,
        error: DataRootMigrationBlockedError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=409,
            code=error.code,
            title="Data Root Migration is blocked",
            detail="Migration admission found one or more blockers.",
            command_id=_command_id(request),
            blockers=data_root_migration_blockers(error),
        )

    @app.exception_handler(DataRootMigrationNotFoundError)
    async def data_root_migration_not_found(
        request: Request,
        error: DataRootMigrationNotFoundError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=404,
            code=error.code,
            title="Data Root Migration not found",
            detail="No migration exists for the requested identifier.",
        )

    async def data_root_migration_conflict(
        request: Request,
        error: Exception,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=409,
            code=str(getattr(error, "code", "data_root_migration_conflict")),
            title="Data Root Migration command rejected",
            detail="The migration command conflicts with durable control state.",
            command_id=_command_id(request),
        )

    for conflict_type in (
        DataRootMigrationConflictError,
        DataRootMigrationIdempotencyError,
        DataRootMigrationCancellationTooLateError,
    ):
        app.add_exception_handler(conflict_type, data_root_migration_conflict)

    async def data_root_unavailable(
        request: Request,
        error: DataRootMaintenanceError | DataRootRestartRequiredError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=503,
            code=error.code,
            title=(
                "CutMaster restart required"
                if isinstance(error, DataRootRestartRequiredError)
                else "Application Data Root maintenance"
            ),
            detail=(
                "Restart CutMaster to use the migrated Application Data Root."
                if isinstance(error, DataRootRestartRequiredError)
                else "The Application Data Root is undergoing maintenance."
            ),
            retryable=True,
        )

    app.add_exception_handler(DataRootMaintenanceError, data_root_unavailable)
    app.add_exception_handler(DataRootRestartRequiredError, data_root_unavailable)

    @app.exception_handler(ApplicationError)
    async def application_error(
        request: Request,
        error: ApplicationError,
    ) -> JSONResponse:
        return problem_response(
            request,
            status=409,
            code=error.code,
            title="Application command rejected",
            detail=str(error),
            command_id=_command_id(request),
        )

    async def invalid_value(request: Request, error: Exception) -> JSONResponse:
        return problem_response(
            request,
            status=422,
            code="invalid_request",
            title="Invalid request",
            detail=str(error),
            command_id=_command_id(request),
        )

    app.add_exception_handler(ValueError, invalid_value)
    app.add_exception_handler(TypeError, invalid_value)

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception) -> JSONResponse:
        LOGGER.exception("Unhandled Web adapter failure", exc_info=error)
        return problem_response(
            request,
            status=500,
            code="internal_error",
            title="Internal server error",
            detail="CutMaster could not complete the request.",
            command_id=_command_id(request),
            retryable=False,
        )


__all__ = ["PROBLEM_MEDIA_TYPE", "install_problem_handlers", "problem_response"]
