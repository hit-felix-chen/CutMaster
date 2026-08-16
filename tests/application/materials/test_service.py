from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest

from cutmaster.application.materials import MaterialView, MaterialsService
from cutmaster.configuration.effective import load_effective_configuration
from cutmaster.domain.materials import MaterialCondition, MaterialType
from cutmaster.infrastructure.storage.local.material_catalog import (
    MaterialCatalog,
    MaterialNameCollisionError,
    MaterialReferencedError,
)


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


def _effective(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text(MINIMAL_CONFIG + "\n", encoding="utf-8")
    return load_effective_configuration(config_path)


def _source(path: Path, value: bytes) -> Path:
    path.write_bytes(value)
    return path


def test_service_constructs_catalog_only_for_first_real_operation(
    tmp_path: Path,
) -> None:
    effective = _effective(tmp_path)
    service = MaterialsService(effective)

    assert not effective.data_root.exists()

    assert service.list() == []

    assert (effective.data_root / "media" / "manifest.json").is_file()


def test_add_and_ensure_return_caller_safe_views(tmp_path: Path) -> None:
    service = MaterialsService(_effective(tmp_path))
    first_source = _source(tmp_path / "first.mp4", b"same video")
    second_source = _source(tmp_path / "second.mp4", b"same video")

    first = service.add(first_source, MaterialType.VIDEO, "Opening Scene")
    second = service.ensure(second_source, "video", " Opening   Scene ")

    assert isinstance(first, MaterialView)
    assert first.material_id == second.material_id
    assert first.material_type is MaterialType.VIDEO
    assert first.name == second.name == "Opening Scene"
    assert first.condition is MaterialCondition.QUEUED
    assert first.reused is False
    assert second.reused is True
    assert {field.name for field in fields(MaterialView)} == {
        "material_id",
        "material_type",
        "name",
        "condition",
        "reused",
    }
    assert not hasattr(first, "fingerprint")
    assert not hasattr(first, "source_path")
    assert not hasattr(first, "memory_root")


def test_type_scoped_queries_and_collision(tmp_path: Path) -> None:
    service = MaterialsService(_effective(tmp_path))
    source = _source(tmp_path / "source.bin", b"bytes")
    video = service.add(source, "video", "Shared")
    music = service.add(source, "music", "Shared")

    assert service.get(video.material_id) == video
    assert service.find_by_name("video", " Shared ") == video
    assert service.find_by_name("music", "Shared") == music
    assert service.find_by_name("video", "Missing") is None
    assert service.list("video") == [video]

    with pytest.raises(MaterialNameCollisionError):
        service.add(source, "video", "Shared")


def test_internal_lease_verifies_and_hides_paths_from_view(tmp_path: Path) -> None:
    service = MaterialsService(_effective(tmp_path))
    source = _source(tmp_path / "score.mp3", b"music")
    view = service.add(source, "music", "Score")

    with service.lease(view.material_id) as binding:
        assert binding.material.material_id == view.material_id
        assert binding.source_path.read_bytes() == b"music"
        assert binding.memory_root.is_dir()
        assert binding.manifest_path.is_file()

    assert service.verify(view.material_id) is True


def test_service_manages_private_subtitle_only_through_a_video_lease(
    tmp_path: Path,
) -> None:
    service = MaterialsService(_effective(tmp_path))
    view = service.add(
        _source(tmp_path / "film.mp4", b"video"),
        "video",
        "Film",
    )
    subtitle = _source(tmp_path / "film.srt", b"subtitle bytes")

    with service.lease(view.material_id) as binding:
        managed = service.ensure_subtitle(binding, subtitle)
        assert managed != subtitle
        assert managed.read_bytes() == b"subtitle bytes"
        assert service.resolve_subtitle(binding) == managed

    current = service.get(view.material_id)
    assert current == view
    assert not hasattr(current, "subtitle_path")
    assert not hasattr(current, "subtitle_fingerprint")


def test_delete_uses_injected_reference_checker_without_boolean_override(
    tmp_path: Path,
) -> None:
    class References:
        values: tuple[str, ...] = ("project:project_1",)

        def references(self, _material_id):
            return self.values

    checker = References()
    effective = _effective(tmp_path)
    catalog = MaterialCatalog(
        effective.data_root / "media",
        reference_checker=checker,
    )
    service = MaterialsService(effective, catalog=catalog)
    view = service.add(
        _source(tmp_path / "clip.mp4", b"video"),
        "video",
        "Clip",
    )

    with pytest.raises(MaterialReferencedError) as raised:
        service.delete(view.material_id)
    assert raised.value.references == ("project:project_1",)
    assert service.get(view.material_id) is not None

    checker.values = ()
    service.delete(view.material_id)
    assert service.get(view.material_id) is None
