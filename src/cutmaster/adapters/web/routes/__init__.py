"""Route groups for the CutMaster Web adapter."""

from cutmaster.adapters.web.routes.activity import router as activity_router
from cutmaster.adapters.web.routes.health import router as health_router
from cutmaster.adapters.web.routes.materials import router as materials_router
from cutmaster.adapters.web.routes.projects import router as projects_router
from cutmaster.adapters.web.routes.review import (
    project_render_router,
    render_router,
    router as review_router,
)
from cutmaster.adapters.web.routes.runs import router as runs_router
from cutmaster.adapters.web.routes.settings import router as settings_router

__all__ = [
    "activity_router",
    "health_router",
    "materials_router",
    "projects_router",
    "project_render_router",
    "render_router",
    "review_router",
    "runs_router",
    "settings_router",
]
