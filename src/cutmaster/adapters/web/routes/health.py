"""Process-health endpoint."""

from fastapi import APIRouter

from cutmaster.adapters.web.dependencies import ApplicationDependency


router = APIRouter(tags=["health"])


@router.get("/health")
def health(application: ApplicationDependency) -> dict[str, object]:
    settings = application.settings.get()
    root_state = application.data_root_coordinator.state()
    return {
        "status": "restart_required" if root_state.restart_required else "ok",
        "service": "cutmaster",
        "data_root": {
            "maintenance": root_state.maintenance,
            "restart_required": root_state.restart_required,
            "migration_id": root_state.migration_id,
            "migration_status": root_state.migration_status,
        },
        "configured": {
            "llm": settings.secrets.llm_configured,
            "vlm": settings.secrets.vlm_configured,
            "asr": settings.secrets.asr_configured,
        },
    }


__all__ = ["router"]
