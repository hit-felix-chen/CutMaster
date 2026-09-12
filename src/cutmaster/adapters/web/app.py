"""FastAPI application factory for local CutMaster Web clients."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from cutmaster.adapters.web.access import can_write

from cutmaster.adapters.web.data_root_migration_supervisor import (
    DataRootMigrationSupervisor,
    LocalDataRootMigrationSupervisor,
    finalize_migration_after_restart,
)
from cutmaster.adapters.web.problem_details import (
    install_problem_handlers,
    problem_response,
)
from cutmaster.adapters.web.routes import (
    activity_router,
    health_router,
    logs_router,
    materials_router,
    project_render_router,
    projects_router,
    render_router,
    review_router,
    runs_router,
    settings_router,
)
from cutmaster.application import CutMasterApplication
from cutmaster.application.ports import JobDispatcher, LifecycleJobSupervisor

DEFAULT_DEV_ORIGINS = (
    "http://127.0.0.1:5173",
    "http://localhost:5173",
)
_SPA_DOCUMENT_CACHE_CONTROL = "no-cache, no-store, must-revalidate"
_REVALIDATED_ASSET_CACHE_CONTROL = "no-cache"
_IMMUTABLE_ASSET_CACHE_CONTROL = "public, max-age=31536000, immutable"


class _DormantJobDispatcher:
    """Leave durable submissions queued in inspection-only/test Web hosts."""

    __slots__ = ()

    def dispatch(self, _submission: object) -> None:
        return


def create_app(
    config_path: Path | str = Path("config.toml"),
    *,
    application: CutMasterApplication | None = None,
    spa_directory: Path | str | None = None,
    allowed_origins: tuple[str, ...] = DEFAULT_DEV_ORIGINS,
    job_dispatcher: JobDispatcher | None = None,
    job_supervisor: LifecycleJobSupervisor | None = None,
    data_root_migration_supervisor: DataRootMigrationSupervisor | None = None,
) -> FastAPI:
    """Create one local Web adapter over one Application composition root."""

    cutmaster = application or CutMasterApplication.open(config_path)
    if job_supervisor is not None and job_dispatcher is not None:
        raise ValueError("job_supervisor cannot be mixed with job_dispatcher")
    if job_supervisor is not None:
        dispatcher: JobDispatcher = job_supervisor
    elif job_dispatcher is not None:
        dispatcher = job_dispatcher
    else:
        dispatcher = _DormantJobDispatcher()
    migration_supervisor = data_root_migration_supervisor or (
        LocalDataRootMigrationSupervisor(cutmaster)
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        finalize_migration_after_restart(cutmaster)
        migration_supervisor.start()
        if job_supervisor is not None:
            job_supervisor.start()
        try:
            yield
        finally:
            if job_supervisor is not None:
                job_supervisor.stop()
            migration_supervisor.stop()

    app = FastAPI(
        title="CutMaster Web API",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.cutmaster_application = cutmaster
    app.state.cutmaster_job_dispatcher = dispatcher
    app.state.cutmaster_data_root_migration_supervisor = migration_supervisor
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key", "Last-Event-ID"],
    )
    install_problem_handlers(app)

    @app.middleware("http")
    async def data_root_maintenance_gate(request: Request, call_next):
        path = request.url.path
        if not path.startswith("/api"):
            return await call_next(request)
        state = cutmaster.data_root_coordinator.state()
        migration_path = path.startswith("/api/settings/storage/migrations")
        health_path = path in {"/api/health", "/api/access"}
        if state.restart_required and not (migration_path or health_path):
            return problem_response(
                request,
                status=503,
                code="data_root_restart_required",
                title="CutMaster restart required",
                detail="Restart CutMaster to open the migrated Application Data Root.",
                retryable=True,
            )
        if state.maintenance and not (migration_path or health_path):
            response = problem_response(
                request,
                status=503,
                code="data_root_maintenance",
                title="Application Data Root maintenance",
                detail="Managed writes are paused while CutMaster moves its data.",
                retryable=True,
            )
            response.headers["Retry-After"] = "5"
            return response
        return await call_next(request)
    @app.middleware("http")
    async def remote_read_only_gate(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"} and not can_write(request):
            return problem_response(
                request,
                status=403,
                code="remote_read_only",
                title="Remote access is read-only",
                detail="Open CutMaster through a loopback address on the server to make changes.",
                retryable=False,
            )
        return await call_next(request)

    @app.get("/api/access")
    def access(request: Request, response: Response) -> dict[str, bool]:
        response.headers["Cache-Control"] = "no-store"
        return {"can_write": can_write(request)}

    for router in (
        health_router,
        logs_router,
        materials_router,
        projects_router,
        runs_router,
        review_router,
        render_router,
        project_render_router,
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
    index = directory / "index.html"
    immutable_assets = _load_vite_asset_paths(directory)

    @app.get("/{spa_path:path}", include_in_schema=False)
    def spa(spa_path: str) -> FileResponse:
        # API misses stay API errors; the browser shell never hides a typo in an
        # HTTP resource URL behind index.html.
        if spa_path == "api" or spa_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="API resource not found")
        requested = directory / spa_path if spa_path else index
        try:
            resolved = requested.resolve()
            resolved.relative_to(directory)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail="Asset not found") from exc
        if not resolved.is_file() or resolved.is_symlink():
            return FileResponse(
                index,
                headers={"Cache-Control": _SPA_DOCUMENT_CACHE_CONTROL},
            )
        if resolved == index:
            cache_control = _SPA_DOCUMENT_CACHE_CONTROL
        elif resolved.relative_to(directory).as_posix() in immutable_assets:
            cache_control = _IMMUTABLE_ASSET_CACHE_CONTROL
        else:
            cache_control = _REVALIDATED_ASSET_CACHE_CONTROL
        return FileResponse(resolved, headers={"Cache-Control": cache_control})


def _load_vite_asset_paths(directory: Path) -> frozenset[str]:
    """Return build outputs whose content-addressed URLs are safe to cache."""

    try:
        manifest = json.loads(
            (directory / ".vite" / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(manifest, dict):
        return frozenset()

    assets: set[str] = set()
    for entry in manifest.values():
        if not isinstance(entry, dict):
            continue
        _add_manifest_asset(assets, entry.get("file"))
        for field in ("css", "assets"):
            values = entry.get(field)
            if isinstance(values, list):
                for value in values:
                    _add_manifest_asset(assets, value)
    return frozenset(assets)


def _add_manifest_asset(assets: set[str], value: object) -> None:
    if not isinstance(value, str):
        return
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        return
    normalized = path.as_posix().lstrip("./")
    if normalized.startswith("assets/"):
        assets.add(normalized)


__all__ = ["DEFAULT_DEV_ORIGINS", "create_app"]
