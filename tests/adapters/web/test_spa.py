from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cutmaster.adapters.web import create_app
from cutmaster.adapters.web import server
from cutmaster.application import CutMasterApplication


def test_spa_serving_restores_routes_without_capturing_api(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    spa = tmp_path / "dist"
    (spa / "assets").mkdir(parents=True)
    (spa / ".vite").mkdir()
    (spa / "index.html").write_text("<html>CutMaster</html>", encoding="utf-8")
    (spa / ".vite/manifest.json").write_text("{}", encoding="utf-8")
    (spa / "assets/app.js").write_text("export {};", encoding="utf-8")

    with TestClient(create_app(application=application, spa_directory=spa)) as client:
        root = client.get("/")
        deep_link = client.get("/materials/video/mat_example")
        asset = client.get("/assets/app.js")
        api_miss = client.get("/api/not-real")

    assert root.status_code == deep_link.status_code == asset.status_code == 200
    assert "CutMaster" in root.text
    assert deep_link.text == root.text
    assert asset.text == "export {};"
    assert api_miss.status_code == 404
    assert api_miss.headers["content-type"].startswith("application/problem+json")


def test_serve_fails_before_starting_when_web_assets_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_resolve_spa_directory", lambda _value: None)

    with pytest.raises(RuntimeError, match="npm --prefix web"):
        server.serve("config.toml", open_browser=False)
