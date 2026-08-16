from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cutmaster.adapters.web import create_app
from cutmaster.application import CutMasterApplication


MINIMAL_CONFIG = """
[llm]
model = "test-llm"
api_key_env = "CUTMASTER_WEB_TEST_LLM_KEY"

[vlm]
model = "test-vlm"
api_key_env = "CUTMASTER_WEB_TEST_VLM_KEY"

[analyser.asr]
api_key_env = "CUTMASTER_WEB_TEST_ASR_KEY"
""".strip()


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(MINIMAL_CONFIG + "\n", encoding="utf-8")
    return path


@pytest.fixture
def application(config_path: Path) -> CutMasterApplication:
    return CutMasterApplication.open(config_path)


@pytest.fixture
def client(application: CutMasterApplication) -> TestClient:
    with TestClient(create_app(application=application)) as value:
        yield value
