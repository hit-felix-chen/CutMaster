"""Process-health endpoint."""

from fastapi import APIRouter

from cutmaster.adapters.web.dependencies import ApplicationDependency


router = APIRouter(tags=["health"])


@router.get("/health")
def health(application: ApplicationDependency) -> dict[str, object]:
    settings = application.settings.get()
    return {
        "status": "ok",
        "service": "cutmaster",
        "configured": {
            "llm": settings.secrets.llm_configured,
            "vlm": settings.secrets.vlm_configured,
            "asr": settings.secrets.asr_configured,
        },
    }


__all__ = ["router"]
