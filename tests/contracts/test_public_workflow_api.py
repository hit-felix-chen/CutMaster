from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from cutmaster.application.workflow import ExecuteManagedWorkflowCommand


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src"


def test_root_exports_application_and_three_workflow_stages() -> None:
    cutmaster = importlib.import_module("cutmaster")
    analyser = importlib.import_module("cutmaster.workflow.analyser")
    application = importlib.import_module("cutmaster.application.cutmaster")
    planners = importlib.import_module("cutmaster.workflow.planners")
    renderer = importlib.import_module("cutmaster.workflow.renderer")

    assert cutmaster.Analyser is analyser.Analyser
    assert cutmaster.Planners is planners.Planners
    assert cutmaster.Renderer is renderer.Renderer
    assert cutmaster.CutMasterApplication is application.CutMasterApplication
    assert set(cutmaster.__all__) == {
        "Analyser",
        "Planners",
        "Renderer",
        "CutMasterApplication",
    }


def test_importing_root_package_does_not_eagerly_load_workflow_stages() -> None:
    code = """
import json
import sys
import cutmaster
prefixes = (
    "cutmaster.workflow.analyser",
    "cutmaster.workflow.planners",
    "cutmaster.workflow.renderer",
    "cv2",
    "librosa",
    "openai",
)
print(json.dumps(sorted(
    name for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)
)))
"""
    env = os.environ.copy()
    current_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(SOURCE_ROOT), current_pythonpath) if part
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=PROJECT_ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == []


def test_console_script_uses_cli_adapter() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as stream:
        project = tomllib.load(stream)
    assert (
        project["project"]["scripts"]["cutmaster"]
        == "cutmaster.adapters.cli.main:main"
    )


def test_managed_workflow_contract_has_no_caller_selected_output() -> None:
    command = ExecuteManagedWorkflowCommand(
        prompt="A story",
        video_path=Path("source.mp4"),
        audio_path=Path("score.mp3"),
        project_name="Managed Project",
    )

    assert not hasattr(command, "output_dir")
    assert not hasattr(command, "overwrite")
    with pytest.raises(TypeError):
        ExecuteManagedWorkflowCommand(  # type: ignore[call-arg]
            prompt="A story",
            video_path=Path("source.mp4"),
            audio_path=Path("score.mp3"),
            project_name="Managed Project",
            output_dir=Path("external"),
        )


def test_removed_legacy_workflow_modules_are_not_importable() -> None:
    for name in (
        "cutmaster.analyser",
        "cutmaster.planners",
        "cutmaster.renderer",
        "cutmaster.orchestrator",
        "cutmaster.runtime",
        "cutmaster.adapters.local_workflow",
        "cutmaster.adapters.web.material_supervisor",
        "cutmaster.adapters.web.render_supervisor",
        "cutmaster.adapters.web.run_supervisor",
        "cutmaster.adapters.web.server",
        "cutmaster.contracts",
        "cutmaster.contracts.workflow",
        "cutmaster.application.direct",
    ):
        try:
            importlib.import_module(name)
        except ModuleNotFoundError:
            continue
        raise AssertionError(f"legacy module is still importable: {name}")
