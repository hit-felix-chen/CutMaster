from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = PROJECT_ROOT / "src" / "cutmaster"


def test_migrated_implementation_files_have_no_legacy_python_module() -> None:
    legacy_runtime_modules = (
        "json_codec.py",
        "media_probe.py",
        "model_gateway.py",
        "observability.py",
        "progress.py",
        "shot_detection.py",
        "workflow_context.py",
    )

    assert not (PACKAGE_ROOT / "timecode.py").exists()
    assert not tuple((PACKAGE_ROOT / "prompting").rglob("*.py"))
    assert not (PACKAGE_ROOT / "adapters/cli/progress.py").exists()
    assert not tuple(
        name
        for name in legacy_runtime_modules
        if (PACKAGE_ROOT / "runtime" / name).exists()
    )


def test_migrated_implementation_files_exist_under_target_owners() -> None:
    expected_paths = (
        "infrastructure/media/ffprobe.py",
        "infrastructure/models/json_codec.py",
        "infrastructure/models/openai_compatible.py",
        "infrastructure/observability/logging.py",
        "infrastructure/observability/progress.py",
        "workflow/prompting/core.py",
        "workflow/prompting/registry.py",
        "workflow/shared/execution_context.py",
        "workflow/shared/shot_detection.py",
        "workflow/shared/timecode.py",
    )

    missing = [
        path for path in expected_paths if not (PACKAGE_ROOT / path).is_file()
    ]
    assert not missing
