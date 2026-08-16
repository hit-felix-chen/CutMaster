"""Secret-free Effective Configuration and storage routes."""

from __future__ import annotations

from fastapi import APIRouter

from cutmaster.adapters.web.dependencies import (
    ApplicationDependency,
    IdempotencyKey,
)
from cutmaster.adapters.web.presenters import settings_view, storage_report_view
from cutmaster.adapters.web.schemas.settings import SaveSettingsBody
from cutmaster.application.settings import SaveSettingsCommand


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
    result = application.settings.save(
        SaveSettingsCommand(command_id, body.overlay)
    )
    return {
        "settings": settings_view(result.settings),
        "restart_required": result.restart_required,
    }


@router.get("/storage")
def storage(application: ApplicationDependency) -> dict[str, object]:
    return storage_report_view(application.settings.storage_report())


__all__ = ["router"]
