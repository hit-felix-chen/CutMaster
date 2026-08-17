"""Outermost composition for the local Web and Worker adapters."""

from __future__ import annotations

import threading
import webbrowser
from pathlib import Path

import uvicorn

from cutmaster.adapters.web.app import _resolve_spa_directory, create_app
from cutmaster.adapters.worker.supervisor import LocalJobSupervisor
from cutmaster.application import CutMasterApplication


def create_local_web_app(
    config_path: Path | str,
    *,
    spa_directory: Path | str | None = None,
):
    """Wire peer Web and Worker adapters around one Application instance."""

    application = CutMasterApplication.open(config_path)
    supervisor = LocalJobSupervisor(application)
    return create_app(
        application=application,
        spa_directory=spa_directory,
        job_supervisor=supervisor,
    )


def serve(
    config_path: Path | str,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    open_browser: bool = True,
) -> None:
    spa_directory = _resolve_spa_directory(None)
    if spa_directory is None:
        raise RuntimeError(
            "CutMaster Web assets are missing. Run "
            "`npm --prefix web ci && npm --prefix web run build` first."
        )
    app = create_local_web_app(config_path, spa_directory=spa_directory)
    if open_browser:
        url = f"http://{host}:{port}/"
        timer = threading.Timer(0.7, webbrowser.open, args=(url,))
        timer.daemon = True
        timer.start()
    uvicorn.run(app, host=host, port=port, log_level="info")


__all__ = ["create_local_web_app", "serve"]
