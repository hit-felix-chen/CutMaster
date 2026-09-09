from __future__ import annotations

from pathlib import Path

import pytest

from cutmaster.configuration.effective import load_effective_configuration

BASE_CONFIG = """
[llm]
model = "test-llm"
api_key_env = "CUTMASTER_TEST_LLM_KEY"
max_tokens = 100

[vlm]
model = "test-vlm"
api_key_env = "CUTMASTER_TEST_VLM_KEY"

[analyser.asr]
api_key_env = "CUTMASTER_TEST_ASR_KEY"

[renderer]
width = 1920
height = 1080
fps = 30
""".strip()


def _write_base(directory: Path, name: str = "config.toml") -> Path:
    path = directory / name
    path.write_text(BASE_CONFIG + "\n", encoding="utf-8")
    return path


def test_effective_configuration_uses_only_the_selected_config_file(
    tmp_path: Path,
) -> None:
    base_path = _write_base(tmp_path)
    (tmp_path / "config.local.toml").write_text(
        """
[llm]
max_tokens = 321

[renderer]
width = 1280
""".strip()
        + "\n",
        encoding="utf-8",
    )

    effective = load_effective_configuration(base_path)
    values = effective.to_dict()

    assert values["llm"]["model"] == "test-llm"
    assert values["llm"]["max_tokens"] == 100
    assert values["renderer"]["width"] == 1920
    assert values["renderer"]["height"] == 1080
    assert values["renderer"]["fps"] == 30
    assert values["renderer"]["encoder"] == "auto"
    assert effective.sources.base_path == base_path


def test_effective_configuration_uses_an_explicit_alternate_config_directly(
    tmp_path: Path,
) -> None:
    base_path = _write_base(tmp_path, "eval.toml")
    (tmp_path / "config.local.toml").write_text(
        """
[llm]
max_tokens = 999
""".strip()
        + "\n",
        encoding="utf-8",
    )

    effective = load_effective_configuration(base_path)

    assert effective.to_dict()["llm"]["max_tokens"] == 100
    assert effective.sources.base_path == base_path


def test_effective_configuration_never_exposes_secret_values(tmp_path: Path) -> None:
    effective = load_effective_configuration(_write_base(tmp_path))
    values = effective.to_dict()

    assert "api_key" not in values["llm"]
    assert "api_key" not in values["analyser"]["asr"]
    assert values["vlm"]["api_key_env"] == "CUTMASTER_TEST_VLM_KEY"
    assert effective.secret_references.llm_api_key_env == "CUTMASTER_TEST_LLM_KEY"
    assert "secret-value" not in effective.canonical_json()


def test_effective_configuration_rejects_inline_api_keys(tmp_path: Path) -> None:
    base_path = _write_base(tmp_path)
    base_path.write_text(
        base_path.read_text(encoding="utf-8").replace(
            'api_key_env = "CUTMASTER_TEST_LLM_KEY"',
            'api_key = "secret-value"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Inline secret llm.api_key"):
        load_effective_configuration(base_path)


def test_effective_configuration_is_immutable_and_loads_without_credentials(
    tmp_path: Path,
) -> None:
    effective = load_effective_configuration(_write_base(tmp_path))

    with pytest.raises(TypeError):
        effective.values["llm"]["model"] = "changed"  # type: ignore[index]


def test_effective_configuration_validates_complete_merged_values(
    tmp_path: Path,
) -> None:
    base_path = _write_base(tmp_path)
    base_path.write_text(
        base_path.read_text(encoding="utf-8").replace(
            "fps = 30",
            "fps = 30\noriginal_volume = 0.5",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="original_volume must be 0"):
        load_effective_configuration(base_path)


@pytest.mark.parametrize(
    ("old", "new", "field_name"),
    [
        ('model = "test-llm"', "model = 123", "llm.model"),
        ("max_tokens = 100", 'max_tokens = "4000"', "llm.max_tokens"),
        (
            'api_key_env = "CUTMASTER_TEST_ASR_KEY"',
            'api_key_env = "CUTMASTER_TEST_ASR_KEY"\ntimeout_sec = "600"',
            "analyser.asr.timeout_sec",
        ),
        ("fps = 30", "fps = true", "renderer.fps"),
        ("fps = 30", "fps = 30\nbgm_volume = nan", "renderer.bgm_volume"),
    ],
)
def test_effective_configuration_rejects_coercible_or_non_finite_types(
    tmp_path: Path,
    old: str,
    new: str,
    field_name: str,
) -> None:
    base_path = _write_base(tmp_path)
    base_path.write_text(
        base_path.read_text(encoding="utf-8").replace(old, new),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match=field_name):
        load_effective_configuration(base_path)


def test_effective_configuration_rejects_unknown_config_keys(
    tmp_path: Path,
) -> None:
    base_path = _write_base(tmp_path)
    base_path.write_text(
        base_path.read_text(encoding="utf-8").replace(
            "max_tokens = 100",
            "max_tokens = 100\nmax_toknes = 321",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=r"Unknown keys in \[llm\]"):
        load_effective_configuration(base_path)


def test_effective_configuration_ignores_legacy_local_overlay(
    tmp_path: Path,
) -> None:
    base_path = _write_base(tmp_path)
    (tmp_path / "config.local.toml").write_text(
        """
[analyser.material_analysis]
material_library_dir = "another-library"
""".strip()
        + "\n",
        encoding="utf-8",
    )

    effective = load_effective_configuration(base_path)

    assert effective.data_root == tmp_path / ".cutmaster"
    assert "material_analysis" not in effective.to_dict()["analyser"]


def test_effective_configuration_rejects_invalid_secret_reference(
    tmp_path: Path,
) -> None:
    base_path = _write_base(tmp_path)
    base_path.write_text(
        base_path.read_text(encoding="utf-8").replace(
            'api_key_env = "CUTMASTER_TEST_LLM_KEY"',
            'api_key_env = "invalid-name"',
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="environment variable name"):
        load_effective_configuration(base_path)


def test_data_root_pointer_must_be_absolute(tmp_path: Path) -> None:
    base_path = _write_base(tmp_path)
    (tmp_path / ".cutmaster-location").write_text(
        "relative/data\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must contain an absolute path"):
        load_effective_configuration(base_path)


def test_data_root_pointer_rejects_non_file_and_filesystem_root(
    tmp_path: Path,
) -> None:
    base_path = _write_base(tmp_path)
    pointer = tmp_path / ".cutmaster-location"
    pointer.mkdir()
    with pytest.raises(ValueError, match="regular file"):
        load_effective_configuration(base_path)
    pointer.rmdir()
    pointer.write_text("/\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Filesystem root"):
        load_effective_configuration(base_path)


def test_data_root_rejects_an_existing_regular_file(tmp_path: Path) -> None:
    base_path = _write_base(tmp_path)
    occupied = tmp_path / "not-a-directory"
    occupied.write_text("data", encoding="utf-8")
    (tmp_path / ".cutmaster-location").write_text(
        f"{occupied}\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="must be a directory"):
        load_effective_configuration(base_path)


def test_data_root_defaults_beside_the_selected_base(tmp_path: Path) -> None:
    effective = load_effective_configuration(_write_base(tmp_path, "eval.toml"))

    assert effective.sources.base_path == tmp_path / "eval.toml"
    assert effective.data_root == tmp_path / ".cutmaster"
    assert not effective.data_root.exists()


def test_data_root_pointer_selects_a_custom_absolute_root(tmp_path: Path) -> None:
    base_path = _write_base(tmp_path)
    custom_root = tmp_path / "elsewhere" / "cutmaster-data"
    (tmp_path / ".cutmaster-location").write_text(
        f"{custom_root}\n",
        encoding="utf-8",
    )

    effective = load_effective_configuration(base_path)

    assert effective.data_root == custom_root
    assert not custom_root.exists()


def test_application_snapshot_materializes_defaults_and_has_one_storage_authority(
    tmp_path: Path,
) -> None:
    effective = load_effective_configuration(_write_base(tmp_path))
    values = effective.to_dict()

    assert values["llm"]["max_retries"] == 3
    assert isinstance(values["llm"]["max_tokens"], int)
    assert values["planners"]["aster_team"]["max_rounds"] == 3
    assert values["planners"]["aster_team"]["max_local_replans"] == 2
    assert "material_analysis" not in values["analyser"]
    assert effective.data_root == tmp_path / ".cutmaster"
