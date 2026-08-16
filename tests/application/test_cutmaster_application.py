from __future__ import annotations

import os
import json
import subprocess
import sys
from pathlib import Path

from cutmaster import CutMasterApplication
from cutmaster.configuration.effective import load_effective_configuration
from cutmaster.application.direct.service import DirectService
from cutmaster.application.jobs.service import JobsService
from cutmaster.application.materials.service import MaterialsService
from cutmaster.application.projects.service import ProjectsService
from cutmaster.application.renders.service import RendersService
from cutmaster.application.runs.service import RunsService
from cutmaster.application.settings.service import SettingsService


MINIMAL_CONFIG = """
[llm]
model = "test-llm"
api_key_env = "CUTMASTER_TEST_LLM_KEY"

[vlm]
model = "test-vlm"
api_key_env = "CUTMASTER_TEST_VLM_KEY"

[analyser.asr]
api_key_env = "CUTMASTER_TEST_ASR_KEY"
""".strip()


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "config.toml"
    config_path.write_text(MINIMAL_CONFIG + "\n", encoding="utf-8")
    return config_path


def test_open_is_side_effect_free_and_does_not_require_credentials(
    tmp_path: Path,
    monkeypatch,
) -> None:
    for name in (
        "CUTMASTER_TEST_LLM_KEY",
        "CUTMASTER_TEST_VLM_KEY",
        "CUTMASTER_TEST_ASR_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    app = CutMasterApplication.open(_write_config(tmp_path))

    assert app.settings.effective_configuration.data_root == tmp_path / ".cutmaster"
    assert not (tmp_path / ".cutmaster").exists()


def test_application_direct_constructor_is_not_a_public_bootstrap_path(
    tmp_path: Path,
) -> None:
    effective = load_effective_configuration(_write_config(tmp_path))

    try:
        CutMasterApplication(effective)
    except TypeError as error:
        assert "Use CutMasterApplication.open" in str(error)
    else:
        raise AssertionError("Direct Application construction unexpectedly succeeded")


def test_application_exposes_seven_concrete_lazy_singleton_services(
    tmp_path: Path,
) -> None:
    app = CutMasterApplication.open(_write_config(tmp_path))
    expected = {
        "direct": DirectService,
        "materials": MaterialsService,
        "projects": ProjectsService,
        "runs": RunsService,
        "renders": RendersService,
        "jobs": JobsService,
        "settings": SettingsService,
    }

    for name, service_type in expected.items():
        first = getattr(app, name)
        second = getattr(app, name)
        assert isinstance(first, service_type)
        assert first is second

    assert not (tmp_path / ".cutmaster").exists()


def test_process_environment_wins_over_sibling_dotenv(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = _write_config(tmp_path)
    (tmp_path / ".env").write_text(
        "CUTMASTER_TEST_LLM_KEY=dotenv-value\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CUTMASTER_TEST_LLM_KEY", "process-value")

    CutMasterApplication.open(config_path)

    assert os.environ["CUTMASTER_TEST_LLM_KEY"] == "process-value"


def test_open_does_not_load_workflow_stages_or_heavy_media_dependencies(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    code = f"""
import json
import sys
from cutmaster import CutMasterApplication

CutMasterApplication.open({str(config_path)!r})
prefixes = (
    "cutmaster.workflow.analyser",
    "cutmaster.workflow.planners",
    "cutmaster.workflow.renderer",
    "cv2",
    "librosa",
    "openai",
)
loaded = sorted(
    name
    for name in sys.modules
    if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes)
)
print(json.dumps(loaded))
"""

    completed = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == []
