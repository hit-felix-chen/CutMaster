"""FastAPI application factory for local CutMaster Web clients."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from cutmaster.application import CutMasterApplication
from cutmaster.adapters.web.problem_details import install_problem_handlers
from cutmaster.adapters.web.run_supervisor import (
    RunDispatcher,
    SubprocessRunDispatcher,
)
from cutmaster.adapters.web.routes import (
    activity_router,
    health_router,
    materials_router,
    projects_router,
    render_media_router,
    review_router,
    runs_router,
    settings_router,
)


DEFAULT_DEV_ORIGINS = (
    "http://127.0.0.1:5173",
    "http://localhost:5173",
)


def create_app(
    config_path: Path | str = Path("config.toml"),
    *,
    application: CutMasterApplication | None = None,
    spa_directory: Path | str | None = None,
    allowed_origins: tuple[str, ...] = DEFAULT_DEV_ORIGINS,
    run_dispatcher: RunDispatcher | None = None,
) -> FastAPI:
    """Create one local Web adapter over one Application composition root."""

    cutmaster = application or CutMasterApplication.open(config_path)
    app = FastAPI(
        title="CutMaster Web API",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.cutmaster_application = cutmaster
    app.state.cutmaster_run_dispatcher = (
        run_dispatcher or SubprocessRunDispatcher(cutmaster)
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key"],
    )
    install_problem_handlers(app)
    for router in (
        health_router,
        materials_router,
        projects_router,
        runs_router,
        review_router,
        render_media_router,
        activity_router,
        settings_router,
    ):
        app.include_router(router, prefix="/api")

    resolved_spa = _resolve_spa_directory(spa_directory)
    if resolved_spa is not None:
        _install_spa_routes(app, resolved_spa)
    return app


def _resolve_spa_directory(value: Path | str | None) -> Path | None:
    if value is not None:
        candidate = Path(value).expanduser().resolve()
        candidates = (candidate,)
    else:
        candidates = (
            Path(__file__).resolve().parents[4] / "web" / "dist",
            Path(__file__).resolve().with_name("static"),
        )
    for candidate in candidates:
        index = candidate / "index.html"
        manifest = candidate / ".vite" / "manifest.json"
        if (
            candidate.is_dir()
            and not candidate.is_symlink()
            and index.is_file()
            and not index.is_symlink()
            and manifest.is_file()
            and not manifest.is_symlink()
        ):
            return candidate
    return None


def _install_spa_routes(app: FastAPI, directory: Path) -> None:
    @app.get("/{spa_path:path}", include_in_schema=False)
    def spa(spa_path: str) -> FileResponse:
        # API misses stay API errors; the browser shell never hides a typo in an
        # HTTP resource URL behind index.html.
        if spa_path == "api" or spa_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API resource not found")
        requested = directory / spa_path if spa_path else directory / "index.html"
        try:
            resolved = requested.resolve()
            resolved.relative_to(directory)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Asset not found") from exc
        if resolved.is_file() and not resolved.is_symlink():
            return FileResponse(resolved)
        return FileResponse(directory / "index.html")


__all__ = ["DEFAULT_DEV_ORIGINS", "create_app"]
