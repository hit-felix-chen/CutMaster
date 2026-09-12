"""Outermost composition for the local Web and Worker adapters."""

from __future__ import annotations

import subprocess
import threading
import webbrowser
from collections.abc import Iterator
from pathlib import Path

import uvicorn

from cutmaster.adapters.web.app import _resolve_spa_directory, create_app
from cutmaster.adapters.worker.supervisor import LocalJobSupervisor
from cutmaster.application import CutMasterApplication


_WEB_BUILD_INPUT_FILES = (
    "index.html",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "tsconfig.app.json",
    "tsconfig.node.json",
    "vite.config.ts",
)
_WEB_BUILD_INPUT_DIRECTORIES = ("public", "src")


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


def _editable_web_root() -> Path | None:
    """Return the Web source root only when this module runs from a checkout."""

    module = Path(__file__).resolve()
    try:
        project_root = module.parents[3]
    except IndexError:
        return None
    source_module = project_root / "src/cutmaster/bootstrap/local_web.py"
    web_root = project_root / "web"
    try:
        is_checkout_module = source_module.is_file() and source_module.samefile(module)
    except OSError:
        return None
    if not is_checkout_module or not (web_root / "package.json").is_file():
        return None
    return web_root


def _iter_web_build_inputs(web_root: Path) -> Iterator[Path]:
    for name in _WEB_BUILD_INPUT_FILES:
        candidate = web_root / name
        if candidate.is_file() and not candidate.is_symlink():
            yield candidate
    for name in _WEB_BUILD_INPUT_DIRECTORIES:
        directory = web_root / name
        if not directory.is_dir() or directory.is_symlink():
            continue
        yield from (
            candidate
            for candidate in directory.rglob("*")
            if candidate.is_file() and not candidate.is_symlink()
        )


def _editable_spa_needs_build(web_root: Path) -> bool:
    distribution = web_root / "dist"
    output_markers = (
        distribution / "index.html",
        distribution / ".vite/manifest.json",
    )
    if any(not output.is_file() or output.is_symlink() for output in output_markers):
        return True
    inputs = tuple(_iter_web_build_inputs(web_root))
    if not inputs:
        return True
    newest_input = max(candidate.stat().st_mtime_ns for candidate in inputs)
    oldest_output = min(candidate.stat().st_mtime_ns for candidate in output_markers)
    return newest_input > oldest_output


def _build_editable_spa(web_root: Path) -> None:
    try:
        subprocess.run(
            ["npm", "run", "build"],
            cwd=web_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        output = ""
        if isinstance(exc, subprocess.CalledProcessError):
            output = (exc.stderr or exc.stdout or "").strip()
        detail = f" Build output: {output[-2000:]}" if output else ""
        raise RuntimeError(
            "CutMaster could not build the Web application automatically."
            f"{detail} Run `npm --prefix web ci` and "
            "`npm --prefix web run build`, then start CutMaster again."
        ) from exc


def _ensure_editable_spa_is_current(web_root: Path) -> Path:
    if _editable_spa_needs_build(web_root):
        _build_editable_spa(web_root)
    distribution = web_root / "dist"
    resolved = _resolve_spa_directory(distribution)
    if resolved is None:
        raise RuntimeError(
            "CutMaster Web assets are incomplete after the automatic build. Run "
            "`npm --prefix web ci` and `npm --prefix web run build`, then start "
            "CutMaster again."
        )
    return resolved


def serve(
    config_path: Path | str,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    open_browser: bool = True,
) -> None:
    editable_web_root = _editable_web_root()
    if editable_web_root is not None:
        spa_directory = _ensure_editable_spa_is_current(editable_web_root)
    else:
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
    uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=False)


__all__ = ["create_local_web_app", "serve"]
