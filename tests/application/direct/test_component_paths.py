from __future__ import annotations

from pathlib import Path

import pytest

import cutmaster.application.direct.service as direct_service_module
from cutmaster.application.direct import (
    AnalyseMusicCommand,
    AnalyseVideoCommand,
    DirectService,
    PlanCommand,
    RenderCommand,
)
from cutmaster.application.materials import MaterialsService
from cutmaster.configuration.effective import load_effective_configuration


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


def _direct(tmp_path: Path) -> tuple[DirectService, Path]:
    config_path = tmp_path / "config.toml"
    config_path.write_text(MINIMAL_CONFIG + "\n", encoding="utf-8")
    effective = load_effective_configuration(config_path)
    return DirectService(effective, MaterialsService(effective)), effective.data_root


@pytest.mark.parametrize(
    "component",
    ("analyser", "analyser/music", "planners", "renderer"),
)
def test_explicit_component_log_is_inside_the_output_directory(
    tmp_path: Path,
    component: str,
) -> None:
    direct, _data_root = _direct(tmp_path)
    output_dir = tmp_path / f"external-{component.replace('/', '-')}"

    paths = direct._component_paths(output_dir, component)

    assert paths.workspace == output_dir.resolve()
    assert paths.log_path == output_dir.resolve() / "cutmaster.log"
    assert paths.workspace.is_dir()


@pytest.mark.parametrize(
    "component",
    ("analyser", "analyser/music", "planners", "renderer"),
)
def test_managed_component_log_is_at_the_direct_bundle_root(
    tmp_path: Path,
    component: str,
) -> None:
    direct, data_root = _direct(tmp_path)

    paths = direct._component_paths(None, component)

    bundle_root = paths.log_path.parent
    assert bundle_root.parent == data_root / "direct"
    assert bundle_root.name.startswith("bundle_")
    assert paths.workspace == bundle_root.joinpath(*component.split("/"))
    assert paths.log_path == bundle_root / "cutmaster.log"
    assert paths.workspace.is_dir()


def _invoke_failing_component(
    direct: DirectService,
    command_name: str,
    tmp_path: Path,
    output_dir: Path,
) -> None:
    if command_name == "analyse":
        direct.analyse_video(
            AnalyseVideoCommand(
                video_path=tmp_path / "missing-video.mp4",
                output_dir=output_dir,
            )
        )
    elif command_name == "analyse-music":
        direct.analyse_music(
            AnalyseMusicCommand(
                audio_path=tmp_path / "missing-music.mp3",
                output_dir=output_dir,
            )
        )
    elif command_name == "plan":
        direct.plan(PlanCommand(prompt="test", output_dir=output_dir))
    elif command_name == "render":
        direct.render(
            RenderCommand(
                plan_path=tmp_path / "missing-plan.json",
                output_dir=output_dir,
            )
        )
    else:  # pragma: no cover - the parameter list is closed below.
        raise AssertionError(f"Unknown component command: {command_name}")


@pytest.mark.parametrize(
    ("command_name", "expected_error"),
    (
        ("analyse", FileNotFoundError),
        ("analyse-music", FileNotFoundError),
        ("plan", ValueError),
        ("render", FileNotFoundError),
    ),
)
def test_component_command_configures_its_explicit_log_before_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command_name: str,
    expected_error: type[Exception],
) -> None:
    direct, _data_root = _direct(tmp_path)
    output_dir = tmp_path / f"{command_name}-output"
    configured: list[Path] = []

    def capture_logging(path: Path, *, console_color: bool) -> None:
        assert console_color is True
        configured.append(path)

    monkeypatch.setattr(
        direct_service_module,
        "configure_logging",
        capture_logging,
    )

    with pytest.raises(expected_error):
        _invoke_failing_component(direct, command_name, tmp_path, output_dir)

    assert configured == [output_dir.resolve() / "cutmaster.log"]
