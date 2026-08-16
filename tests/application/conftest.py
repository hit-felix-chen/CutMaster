from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cutmaster.configuration.effective import EffectiveConfiguration, load_effective_configuration


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def managed_configuration(tmp_path: Path) -> EffectiveConfiguration:
    config_path = tmp_path / "config.toml"
    shutil.copyfile(PROJECT_ROOT / "config.toml", config_path)
    return load_effective_configuration(config_path)

