from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from cutmaster.application.settings import SaveSettingsCommand, SettingsService
from cutmaster.configuration.effective import EffectiveConfiguration


def command_id() -> str:
    return str(uuid4())


def test_settings_save_writes_sparse_overlay_atomically_and_reloads(
    managed_configuration: EffectiveConfiguration,
) -> None:
    service = SettingsService(managed_configuration)

    saved = service.save(
        SaveSettingsCommand(
            command_id(),
            {"renderer": {"width": 1280, "height": 720, "fps": 24}},
        )
    )

    assert saved.restart_required
    assert saved.settings.values["renderer"]["width"] == 1280
    overlay = managed_configuration.sources.overlay_path
    assert overlay.read_text(encoding="utf-8") == (
        "[renderer]\nfps = 24\nheight = 720\nwidth = 1280\n"
    )
    assert overlay.stat().st_mode & 0o777 == 0o600
    assert not list(overlay.parent.glob(f".{overlay.name}.*.tmp"))


def test_invalid_settings_candidate_preserves_previous_bytes(
    managed_configuration: EffectiveConfiguration,
) -> None:
    overlay = managed_configuration.sources.overlay_path
    overlay.write_text("[renderer]\nwidth = 1280\n", encoding="utf-8")
    before = overlay.read_bytes()
    service = SettingsService(managed_configuration)

    with pytest.raises((TypeError, ValueError)):
        service.save(
            SaveSettingsCommand(
                command_id(),
                {"renderer": {"width": "not-an-integer"}},
            )
        )

    assert overlay.read_bytes() == before


def test_settings_replay_detects_overlay_changed_outside_application(
    managed_configuration: EffectiveConfiguration,
) -> None:
    service = SettingsService(managed_configuration)
    identifier = command_id()
    command = SaveSettingsCommand(identifier, {"renderer": {"width": 1280}})
    service.save(command)
    managed_configuration.sources.overlay_path.write_text(
        "[renderer]\nwidth = 1920\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="no longer matches"):
        service.save(command)


def test_storage_report_counts_real_managed_categories(
    managed_configuration: EffectiveConfiguration,
) -> None:
    root = managed_configuration.data_root
    (root / "media/video").mkdir(parents=True)
    (root / "media/video/source.mp4").write_bytes(b"video")
    (root / "projects/p").mkdir(parents=True)
    (root / "projects/p/plan.json").write_bytes(b"{}")
    (root / "direct/bundle_one").mkdir(parents=True)
    (root / "direct/bundle_one/result.json").write_bytes(b"{}")
    (root / "logs").mkdir(parents=True)
    (root / "logs/app.log").write_bytes(b"log")
    service = SettingsService(managed_configuration)

    report = service.storage_report()
    categories = {item.name: item for item in report.categories}

    assert report.direct_bundle_count == 1
    assert categories["materials"].size_bytes == 5
    assert categories["projects"].file_count == 1
    assert categories["direct"].file_count == 1
    assert categories["logs"].size_bytes == 3
    assert categories["database"].file_count == 0
    assert report.total_size_bytes == sum(item.size_bytes for item in report.categories)
