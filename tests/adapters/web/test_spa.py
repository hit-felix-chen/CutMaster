from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest
from fastapi.testclient import TestClient

from cutmaster.adapters.web import create_app
from cutmaster.bootstrap import local_web as server
from cutmaster.application import CutMasterApplication


def test_spa_serving_restores_routes_without_capturing_api(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    spa = tmp_path / "dist"
    (spa / "assets").mkdir(parents=True)
    (spa / ".vite").mkdir()
    (spa / "index.html").write_text("<html>CutMaster</html>", encoding="utf-8")
    (spa / ".vite/manifest.json").write_text(
        json.dumps(
            {
                "index.html": {
                    "file": "assets/app-A1b2C3d4.js",
                    "isEntry": True,
                }
            }
        ),
        encoding="utf-8",
    )
    (spa / "assets/app.js").write_text("export {};", encoding="utf-8")
    (spa / "assets/app-A1b2C3d4.js").write_text(
        "export const version = 1;",
        encoding="utf-8",
    )

    with TestClient(create_app(application=application, spa_directory=spa)) as client:
        root = client.get("/")
        deep_link = client.get("/materials/video/mat_example")
        asset = client.get("/assets/app.js")
        fingerprinted_asset = client.get("/assets/app-A1b2C3d4.js")
        api_miss = client.get("/api/not-real")

    assert (
        root.status_code
        == deep_link.status_code
        == asset.status_code
        == fingerprinted_asset.status_code
        == 200
    )
    assert "CutMaster" in root.text
    assert deep_link.text == root.text
    assert asset.text == "export {};"
    assert root.headers["cache-control"] == "no-cache, no-store, must-revalidate"
    assert (
        deep_link.headers["cache-control"]
        == "no-cache, no-store, must-revalidate"
    )
    assert asset.headers["cache-control"] == "no-cache"
    assert (
        fingerprinted_asset.headers["cache-control"]
        == "public, max-age=31536000, immutable"
    )
    assert api_miss.status_code == 404
    assert api_miss.headers["content-type"].startswith("application/problem+json")


def test_serve_fails_before_starting_when_web_assets_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_editable_web_root", lambda: None)
    monkeypatch.setattr(server, "_resolve_spa_directory", lambda _value: None)

    with pytest.raises(RuntimeError, match="npm --prefix web"):
        server.serve("config.toml", open_browser=False)


def test_local_web_bootstrap_wires_peer_adapters_around_one_application(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    application = object()
    supervisor = object()
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        server,
        "CutMasterApplication",
        type(
            "ApplicationFactory",
            (),
            {"open": staticmethod(lambda _path: application)},
        ),
    )

    def create_supervisor(exact_application):
        assert exact_application is application
        return supervisor

    monkeypatch.setattr(server, "LocalJobSupervisor", create_supervisor)

    def create(**kwargs):
        observed.update(kwargs)
        return "fastapi-app"

    monkeypatch.setattr(server, "create_app", create)
    spa = tmp_path / "dist"

    assert (
        server.create_local_web_app("config.toml", spa_directory=spa)
        == "fastapi-app"
    )
    assert observed == {
        "application": application,
        "spa_directory": spa,
        "job_supervisor": supervisor,
    }


def _write_web_checkout(web: Path, *, source_mtime_ns: int, dist_mtime_ns: int) -> None:
    (web / "src").mkdir(parents=True)
    (web / "dist/.vite").mkdir(parents=True)
    inputs = {
        web / "package.json": "{}",
        web / "package-lock.json": "{}",
        web / "vite.config.ts": "export default {};",
        web / "index.html": "<div id='root'></div>",
        web / "src/main.tsx": "export {};",
    }
    outputs = {
        web / "dist/index.html": "<div id='root'></div>",
        web / "dist/.vite/manifest.json": "{}",
    }
    for path, content in inputs.items():
        path.write_text(content, encoding="utf-8")
        os.utime(path, ns=(source_mtime_ns, source_mtime_ns))
    for path, content in outputs.items():
        path.write_text(content, encoding="utf-8")
        os.utime(path, ns=(dist_mtime_ns, dist_mtime_ns))


def test_serve_builds_stale_editable_spa_before_creating_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    web = tmp_path / "web"
    _write_web_checkout(web, source_mtime_ns=20, dist_mtime_ns=10)
    events: list[str] = []

    monkeypatch.setattr(server, "_editable_web_root", lambda: web, raising=False)

    def run_build(command, **kwargs):
        assert command == ["npm", "run", "build"]
        assert kwargs["cwd"] == web
        events.append("build")
        for output in (web / "dist/index.html", web / "dist/.vite/manifest.json"):
            os.utime(output, ns=(30, 30))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr(server.subprocess, "run", run_build, raising=False)

    def create_app(_config_path, *, spa_directory):
        assert spa_directory == web / "dist"
        events.append("create-app")
        return object()

    monkeypatch.setattr(server, "create_local_web_app", create_app)
    monkeypatch.setattr(server.uvicorn, "run", lambda *_args, **_kwargs: None)

    server.serve("config.toml", open_browser=False)

    assert events == ["build", "create-app"]


def test_serve_does_not_rebuild_fresh_editable_spa(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    web = tmp_path / "web"
    _write_web_checkout(web, source_mtime_ns=10, dist_mtime_ns=20)
    monkeypatch.setattr(server, "_editable_web_root", lambda: web, raising=False)
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("fresh assets must not be rebuilt"),
        raising=False,
    )
    monkeypatch.setattr(server, "create_local_web_app", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server.uvicorn, "run", lambda *_args, **_kwargs: None)

    server.serve("config.toml", open_browser=False)


def test_serve_uses_embedded_wheel_spa_without_invoking_npm(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    embedded = tmp_path / "cutmaster/adapters/web/static"
    (embedded / ".vite").mkdir(parents=True)
    (embedded / "index.html").write_text("index", encoding="utf-8")
    (embedded / ".vite/manifest.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server, "_editable_web_root", lambda: None, raising=False)
    monkeypatch.setattr(server, "_resolve_spa_directory", lambda _value: embedded)
    monkeypatch.setattr(
        server.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("wheel assets must not invoke npm"),
        raising=False,
    )
    monkeypatch.setattr(server, "create_local_web_app", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(server.uvicorn, "run", lambda *_args, **_kwargs: None)

    server.serve("config.toml", open_browser=False)


def test_serve_reports_actionable_error_when_editable_spa_build_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    web = tmp_path / "web"
    (web / "src").mkdir(parents=True)
    (web / "package.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server, "_editable_web_root", lambda: web, raising=False)

    def fail_build(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "npm", stderr="vite failed")

    monkeypatch.setattr(server.subprocess, "run", fail_build, raising=False)

    with pytest.raises(RuntimeError) as caught:
        server.serve("config.toml", open_browser=False)

    message = str(caught.value)
    assert "vite failed" in message
    assert "npm --prefix web ci" in message
    assert "npm --prefix web run build" in message
