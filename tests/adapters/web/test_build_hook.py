from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def load_hook_module():
    spec = importlib.util.spec_from_file_location(
        "cutmaster_hatch_build",
        PROJECT_ROOT / "hatch_build.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load hatch_build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_distribution(path: Path) -> None:
    load_hook_module()._validate_distribution(path)


def test_build_hook_requires_complete_vite_distribution(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="npm --prefix web run build"):
        validate_distribution(tmp_path / "dist")

    distribution = tmp_path / "dist"
    (distribution / ".vite").mkdir(parents=True)
    (distribution / "assets").mkdir()
    (distribution / "index.html").write_text("index", encoding="utf-8")
    (distribution / ".vite/manifest.json").write_text("{}", encoding="utf-8")
    (distribution / "assets/app.js").write_text("", encoding="utf-8")

    validate_distribution(distribution)


def test_build_hook_skips_spa_requirement_for_editable_install() -> None:
    module = load_hook_module()
    hook = object.__new__(module.CustomBuildHook)

    hook.initialize("editable", {})


def test_built_wheel_serves_its_embedded_spa(
    tmp_path: Path,
    config_path: Path,
) -> None:
    wheel_directory = tmp_path / "wheel"
    wheel_directory.mkdir()
    subprocess.run(
        [
            sys.executable,
            "-m",
            "hatchling",
            "build",
            "-t",
            "wheel",
            "-d",
            str(wheel_directory),
        ],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    wheel = next(wheel_directory.glob("cutmaster-*.whl"))
    installed = tmp_path / "installed"
    installed.mkdir()
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed)

    code = f"""
from fastapi.testclient import TestClient
from cutmaster.adapters.web import create_app
from cutmaster.adapters.web.app import _resolve_spa_directory

assets = _resolve_spa_directory(None)
assert assets is not None
assert 'cutmaster/adapters/web/static' in assets.as_posix()
with TestClient(create_app({str(config_path)!r})) as client:
    response = client.get('/')
assert response.status_code == 200
assert '<div id=\"root\"></div>' in response.text
print(assets)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(installed)
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "cutmaster/adapters/web/static" in completed.stdout
