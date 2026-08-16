"""FastAPI application factory for local CutMaster Web clients."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from cutmaster.adapters.web.job_supervisor import JobSupervisor, LocalJobSupervisor
from cutmaster.adapters.web.data_root_migration_supervisor import (
    DataRootMigrationSupervisor,
    LocalDataRootMigrationSupervisor,
    finalize_migration_after_restart,
)
from cutmaster.adapters.web.material_supervisor import (
    MaterialDispatcher,
    SubprocessMaterialDispatcher,
)
from cutmaster.adapters.web.problem_details import (
    install_problem_handlers,
    problem_response,
)
from cutmaster.adapters.web.render_supervisor import (
    RenderDispatcher,
    SubprocessRenderDispatcher,
)
from cutmaster.adapters.web.routes import (
    activity_router,
    health_router,
    materials_router,
    project_render_router,
    projects_router,
    render_router,
    review_router,
    runs_router,
    settings_router,
)
from cutmaster.adapters.web.run_supervisor import (
    RunDispatcher,
    SubprocessRunDispatcher,
)
from cutmaster.application import CutMasterApplication

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
    material_dispatcher: MaterialDispatcher | None = None,
    run_dispatcher: RunDispatcher | None = None,
    render_dispatcher: RenderDispatcher | None = None,
    job_supervisor: JobSupervisor | None = None,
    data_root_migration_supervisor: DataRootMigrationSupervisor | None = None,
    enable_job_supervisor: bool = True,
    job_max_concurrency: int = 2,
    job_poll_interval_sec: float = 0.25,
    job_orphan_after_sec: float = 45.0,
    job_orphan_audit_interval_sec: float = 10.0,
) -> FastAPI:
    """Create one local Web adapter over one Application composition root."""

    cutmaster = application or CutMasterApplication.open(config_path)
    custom_dispatcher = any(
        dispatcher is not None
        for dispatcher in (material_dispatcher, run_dispatcher, render_dispatcher)
    )
    if job_supervisor is not None and not enable_job_supervisor:
        raise ValueError("job_supervisor requires enable_job_supervisor=True")
    if job_supervisor is not None and custom_dispatcher:
        raise ValueError("job_supervisor cannot be mixed with custom dispatchers")
    supervisor = job_supervisor
    if supervisor is None and enable_job_supervisor and not custom_dispatcher:
        supervisor = LocalJobSupervisor(
            cutmaster,
            max_concurrency=job_max_concurrency,
            poll_interval_sec=job_poll_interval_sec,
            orphan_after_sec=job_orphan_after_sec,
            orphan_audit_interval_sec=job_orphan_audit_interval_sec,
        )
    migration_supervisor = data_root_migration_supervisor or (
        LocalDataRootMigrationSupervisor(cutmaster)
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        finalize_migration_after_restart(cutmaster)
        migration_supervisor.start()
        if supervisor is not None:
            supervisor.start()
        try:
            yield
        finally:
            if supervisor is not None:
                supervisor.stop()
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
    app.state.cutmaster_job_supervisor = supervisor
    app.state.cutmaster_data_root_migration_supervisor = migration_supervisor
    if supervisor is not None:
        app.state.cutmaster_material_dispatcher = supervisor
        app.state.cutmaster_run_dispatcher = supervisor
        app.state.cutmaster_render_dispatcher = supervisor
    else:
        app.state.cutmaster_material_dispatcher = (
            material_dispatcher or SubprocessMaterialDispatcher(cutmaster)
        )
        app.state.cutmaster_run_dispatcher = run_dispatcher or SubprocessRunDispatcher(
            cutmaster
        )
        app.state.cutmaster_render_dispatcher = (
            render_dispatcher or SubprocessRenderDispatcher(cutmaster)
        )
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
        health_path = path == "/api/health"
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
    for router in (
        health_router,
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
