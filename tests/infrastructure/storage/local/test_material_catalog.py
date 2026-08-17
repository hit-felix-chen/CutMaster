import hashlib
import json
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Thread
from uuid import UUID

import cutmaster.infrastructure.storage.local.material_catalog as material_catalog_module
import pytest

from cutmaster.application.ports.material_catalog import MaterialRecord
from cutmaster.domain.materials import MaterialCondition, MaterialType
from cutmaster.infrastructure.storage.local.material_catalog import (
    InvalidAnalysisResultError,
    InvalidManifestError,
    InvalidMaterialNameError,
    InvalidSubtitleSidecarError,
    MaterialCatalog,
    MaterialCatalogError,
    MaterialInconsistentError,
    MaterialLeaseRequiredError,
    MaterialNameCollisionError,
    MaterialNotFoundError,
    MaterialReferencedError,
    MaterialConsumedError,
    SubtitleSidecarConflictError,
    fingerprint_file,
    normalize_material_name,
)


def _source(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def _binding(catalog: MaterialCatalog, record: MaterialRecord):
    with catalog.lease(record.material.material_id) as binding:
        return binding


def _analysis_result(record: MaterialRecord) -> dict[str, object]:
    material = record.material
    result: dict[str, object] = {
        "schema_version": "3.0",
        "status": "success",
        "material_id": str(material.material_id),
        "material_type": material.material_type.value,
        "material_name": material.name,
        "material_fingerprint": str(material.fingerprint),
        "memory_schema_version": (
            "3.0" if material.material_type is MaterialType.VIDEO else "2.0"
        ),
        "elapsed_sec": 1.0,
        "material_reused": False,
        "analysis_reused": False,
    }
    if material.material_type is MaterialType.VIDEO:
        result["model_usage_summary"] = {}
        result["model_usage_cumulative_summary"] = {}
    return result


def test_add_uses_normalized_default_name_and_managed_immutable_copy(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path / "  Opening   Scene  .mp4", b"video bytes")
    library = MaterialCatalog(tmp_path / "library")

    record = library.add(source, MaterialType.VIDEO)
    material = record.material
    binding = _binding(library, record)
    source.write_bytes(b"changed outside the library")

    assert material.name == "Opening Scene"
    assert str(material.material_id).startswith("mat_")
    assert UUID(str(material.material_id).removeprefix("mat_")).version == 4
    assert material.material_type is MaterialType.VIDEO
    assert str(material.fingerprint) == hashlib.sha256(b"video bytes").hexdigest()
    assert len(str(material.fingerprint)) == 64
    assert binding.source_path.read_bytes() == b"video bytes"
    assert binding.source_path != source
    assert binding.source_path.relative_to(library.root).parts[:2] == (
        "video",
        str(material.material_id),
    )
    assert binding.source_path.stat().st_mode & stat.S_IWUSR == 0
    assert stat.S_IMODE(library.root.stat().st_mode) == 0o700
    assert stat.S_IMODE((library.root / "video").stat().st_mode) == 0o700
    assert stat.S_IMODE(binding.source_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(binding.memory_root.stat().st_mode) == 0o700
    assert stat.S_IMODE(binding.source_path.stat().st_mode) == 0o400
    assert binding.memory_root.is_dir()
    assert record.reused is False
    assert library.verify(material.material_id) is True

    manifest = json.loads(library.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["materials"][0]["material_id"] == str(material.material_id)
    assert manifest["materials"][0]["name"] == "Opening Scene"
    assert manifest["materials"][0]["fingerprint"] == str(material.fingerprint)
    assert str(source) not in library.manifest_path.read_text(encoding="utf-8")


def test_material_directory_uses_only_opaque_internal_id(
    tmp_path: Path,
) -> None:
    library = MaterialCatalog(tmp_path / "library")

    first = library.add(
        _source(tmp_path / "readable.mp4", b"readable"),
        "video",
        "Opening Scene",
    )
    second = library.add(
        _source(tmp_path / "unsafe.mp4", b"unsafe"),
        "video",
        "Act 1 / Arrival",
    )

    first_binding = _binding(library, first)
    second_binding = _binding(library, second)
    assert first_binding.source_path.parent.name == str(first.material.material_id)
    assert second_binding.source_path.parent.name == str(second.material.material_id)
    assert first.material.material_id != second.material.material_id
    assert "Opening Scene" not in first_binding.source_path.as_posix()
    assert "Act 1" not in second_binding.source_path.as_posix()
    assert str(first.material.fingerprint) not in first_binding.source_path.as_posix()
    assert second_binding.source_path.parent.parent == library.root / "video"


def test_add_rejects_name_collision_while_ensure_reuses_exact_binding(
    tmp_path: Path,
) -> None:
    first_source = _source(tmp_path / "one.mp4", b"same")
    second_source = _source(tmp_path / "two.mp4", b"same")
    library = MaterialCatalog(tmp_path / "library")

    first = library.add(first_source, "video", name="  Launch\tFilm ")
    with pytest.raises(MaterialNameCollisionError, match="already exists"):
        library.add(second_source, "VIDEO", name="Launch Film")
    second = library.ensure(second_source, "VIDEO", name="Launch Film")

    assert second.material.name == first.material.name == "Launch Film"
    assert second.material.material_id == first.material.material_id
    assert _binding(library, second).source_path == _binding(library, first).source_path
    assert second.reused is True
    assert len(library.list()) == 1


def test_same_name_with_new_bytes_is_a_collision_without_numeric_suffix(
    tmp_path: Path,
) -> None:
    library = MaterialCatalog(tmp_path / "library")
    first = library.add(_source(tmp_path / "a.mp4", b"a"), "video", "Trailer")
    for source in (
        _source(tmp_path / "b.mp4", b"b"),
        _source(tmp_path / "c.mp4", b"c"),
    ):
        with pytest.raises(MaterialNameCollisionError):
            library.ensure(source, "video", "Trailer")

    assert [item.material.name for item in library.list("video")] == ["Trailer"]
    assert first.material.name == "Trailer"
    assert library.find_by_name("video", "Trailer") is not None
    assert library.find_by_name("video", "Another Trailer") is None


def test_equal_fingerprint_with_different_candidate_name_or_type_is_distinct(
    tmp_path: Path,
) -> None:
    library = MaterialCatalog(tmp_path / "library")
    source = _source(tmp_path / "media.bin", b"identical")

    video_a = library.add(source, "video", "A")
    video_b = library.add(source, "video", "B")
    music_a = library.add(source, "music", "A")

    assert (
        video_a.material.fingerprint
        == video_b.material.fingerprint
        == music_a.material.fingerprint
    )
    assert len(
        {
            video_a.material.material_id,
            video_b.material.material_id,
            music_a.material.material_id,
        }
    ) == 3
    assert _binding(library, video_a).source_path != _binding(
        library, video_b
    ).source_path
    assert _binding(library, video_a).source_path != _binding(
        library, music_a
    ).source_path
    assert [item.material.name for item in library.list("video")] == ["A", "B"]
    assert [item.material.name for item in library.list("music")] == ["A"]


def test_resolve_uses_type_and_normalized_public_name(tmp_path: Path) -> None:
    library = MaterialCatalog(tmp_path / "library")
    source = _source(tmp_path / "source.mov", b"bytes")
    video = library.add(source, "video", "Shared Name")
    music = library.add(source, "music", "Shared Name")

    assert library.find_by_name("video", " Shared   Name ") == video
    assert library.find_by_name("music", "Shared Name") == music
    assert library.find_by_name("video", "missing") is None
    with pytest.raises(ValueError, match="Unsupported material type"):
        library.find_by_name("image", "Shared Name")


def test_verify_detects_changed_managed_source(tmp_path: Path) -> None:
    library = MaterialCatalog(tmp_path / "library")
    source = _source(tmp_path / "source.mp3", b"music")
    record = library.add(source, "music", "Score")
    binding = _binding(library, record)
    binding.source_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    binding.source_path.write_bytes(b"tampered")

    assert library.verify(record.material.material_id) is False
    with pytest.raises(MaterialInconsistentError):
        library.require_consistent(record.material.material_id)
    with pytest.raises(MaterialInconsistentError):
        library.add(source, "music", "Score")
    with pytest.raises(MaterialInconsistentError):
        library.add(binding.source_path, "music", "Score")
    assert [item.material.name for item in library.list("music")] == ["Score"]


def test_delete_rejects_referenced_material_then_removes_unreferenced_one(
    tmp_path: Path,
) -> None:
    class References:
        values: tuple[str, ...] = ("project:project_1",)

        def references(self, _material_id):
            return self.values

    checker = References()
    library = MaterialCatalog(
        tmp_path / "library",
        reference_checker=checker,
    )
    record = library.add(
        _source(tmp_path / "source.mp4", b"video"), "video", "Clip"
    )
    binding = _binding(library, record)

    with pytest.raises(MaterialReferencedError):
        library.delete(record.material.material_id)
    assert binding.source_path.exists()

    checker.values = ()
    library.delete(record.material.material_id)

    assert not binding.source_path.exists()
    assert library.list() == []
    assert library.get(record.material.material_id) is None


def test_consumption_leases_share_and_delete_rejects_without_waiting(
    tmp_path: Path,
) -> None:
    library = MaterialCatalog(tmp_path / "library")
    record = library.add(
        _source(tmp_path / "source.mp4", b"video"), "video", "Clip"
    )
    entered = Event()
    release = Event()

    def consume() -> None:
        with library.consume_lease(record.material.material_id):
            entered.set()
            release.wait(2)

    worker = Thread(target=consume)
    worker.start()
    assert entered.wait(1)
    try:
        started = time.monotonic()
        with library.consume_lease(record.material.material_id):
            pass
        assert time.monotonic() - started < 0.25

        started = time.monotonic()
        with pytest.raises(MaterialConsumedError):
            library.delete(record.material.material_id)
        assert time.monotonic() - started < 0.25
    finally:
        release.set()
        worker.join(timeout=2)

    library.delete(record.material.material_id)


def test_inspection_does_not_wait_for_exclusive_analysis_lease(tmp_path: Path) -> None:
    library = MaterialCatalog(tmp_path / "library")
    record = library.add(
        _source(tmp_path / "source.mp4", b"video"), "video", "Clip"
    )
    analysis_entered = Event()
    release_analysis = Event()
    inspection_finished = Event()

    def analyse() -> None:
        with library.lease(record.material.material_id):
            analysis_entered.set()
            release_analysis.wait(2)

    def inspect() -> None:
        with library.read_lease(record.material.material_id) as binding:
            assert binding.source_path.is_file()
        inspection_finished.set()

    analysis = Thread(target=analyse)
    inspection = Thread(target=inspect)
    analysis.start()
    assert analysis_entered.wait(1)
    inspection.start()
    try:
        assert inspection_finished.wait(0.25)
    finally:
        release_analysis.set()
        analysis.join(timeout=2)
        inspection.join(timeout=2)


def test_normalization_rejects_empty_reserved_and_control_names() -> None:
    assert normalize_material_name("  Ｍｙ　Film  ") == "My Film"
    for value in ("", " \t ", ".", "..", "bad\x00name"):
        with pytest.raises(InvalidMaterialNameError):
            normalize_material_name(value)


def test_fingerprint_file_hashes_all_bytes(tmp_path: Path) -> None:
    content = b"0123456789" * 300_000
    source = _source(tmp_path / "large.bin", content)

    assert fingerprint_file(source, chunk_size=17) == hashlib.sha256(
        content
    ).hexdigest()


def test_concurrent_additions_do_not_lose_manifest_records(tmp_path: Path) -> None:
    library_root = tmp_path / "library"
    sources = [
        _source(tmp_path / f"source-{index}.mp4", f"video-{index}".encode())
        for index in range(12)
    ]
    barrier = Barrier(len(sources))

    def add(index: int) -> str:
        library = MaterialCatalog(library_root)
        barrier.wait()
        return library.add(
            sources[index],
            "video",
            f"Material {index}",
        ).material.name

    with ThreadPoolExecutor(max_workers=len(sources)) as executor:
        returned = list(executor.map(add, range(len(sources))))

    catalog = MaterialCatalog(library_root)
    persisted = catalog.list("video")
    assert sorted(returned) == sorted(
        f"Material {index}" for index in range(12)
    )
    assert sorted(record.material.name for record in persisted) == sorted(returned)
    assert len({record.material.material_id for record in persisted}) == len(sources)
    assert all(_binding(catalog, record).source_path.is_file() for record in persisted)


def test_allocator_skips_orphaned_material_id_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:
    library = MaterialCatalog(tmp_path / "library")
    orphan_uuid = UUID("00000000-0000-4000-8000-000000000001")
    allocated_uuid = UUID("00000000-0000-4000-8000-000000000002")
    orphan_id = f"mat_{orphan_uuid}"
    (library.root / "video" / orphan_id).mkdir(parents=True)
    values = iter((orphan_uuid, allocated_uuid))
    monkeypatch.setattr(material_catalog_module, "uuid4", lambda: next(values))

    record = library.add(
        _source(tmp_path / "source.mp4", b"video"),
        "video",
        "Film",
    )

    assert str(record.material.material_id) == f"mat_{allocated_uuid}"
    assert (library.root / "video" / orphan_id).is_dir()


def test_manifest_rejects_legacy_schema_invalid_and_duplicate_ids(
    tmp_path: Path,
) -> None:
    legacy_root = tmp_path / "legacy"
    legacy_root.mkdir()
    (legacy_root / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "materials": []}),
        encoding="utf-8",
    )
    with pytest.raises(InvalidManifestError, match="Unsupported"):
        MaterialCatalog(legacy_root)

    library = MaterialCatalog(tmp_path / "library")
    library.add(_source(tmp_path / "source.mp4", b"video"), "video", "Film")
    manifest = json.loads(library.manifest_path.read_text(encoding="utf-8"))
    duplicate = dict(manifest["materials"][0])
    duplicate["name"] = "Another Film"
    manifest["materials"].append(duplicate)
    library.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(InvalidManifestError, match="Duplicate Material ID"):
        MaterialCatalog(library.root)


def test_new_library_does_not_touch_legacy_backup(tmp_path: Path) -> None:
    backup = tmp_path / ".cutmaster" / "materials-backup"
    backup.mkdir(parents=True)
    marker = backup / "marker.bin"
    marker.write_bytes(b"legacy bytes")

    library = MaterialCatalog(tmp_path / ".cutmaster" / "media")
    library.add(_source(tmp_path / "source.mp3", b"music"), "music", "Score")

    assert marker.read_bytes() == b"legacy bytes"
    assert list(backup.iterdir()) == [marker]


def test_lease_does_not_recreate_a_deleted_material(tmp_path: Path) -> None:
    library = MaterialCatalog(tmp_path / "library")
    record = library.add(
        _source(tmp_path / "source.mp4", b"video"),
        "video",
        "Film",
    )
    material_directory = _binding(library, record).source_path.parent
    library.delete(record.material.material_id)

    with pytest.raises(MaterialNotFoundError):
        with library.lease(record.material.material_id):
            pass

    assert not material_directory.exists()


def test_add_rejects_symlinked_type_directory_without_writing_outside(
    tmp_path: Path,
) -> None:
    library = MaterialCatalog(tmp_path / "library")
    video_directory = library.root / "video"
    video_directory.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    video_directory.symlink_to(outside, target_is_directory=True)

    with pytest.raises(MaterialCatalogError, match="cannot be a symlink"):
        library.add(
            _source(tmp_path / "source.mp4", b"video"),
            "video",
            "Film",
        )

    assert list(outside.iterdir()) == []


def test_analysis_lease_serializes_work_for_one_material(
    tmp_path: Path,
) -> None:
    library = MaterialCatalog(tmp_path / "library")
    record = library.add(
        _source(tmp_path / "source.mp4", b"video"),
        "video",
        "Film",
    )
    attempted = Event()
    entered = Event()
    lease = library.lease

    def contender() -> None:
        attempted.set()
        with lease(record.material.material_id):
            entered.set()

    with lease(record.material.material_id):
        thread = Thread(target=contender)
        thread.start()
        assert attempted.wait(timeout=1.0)
        assert not entered.wait(timeout=0.05)
    thread.join(timeout=1.0)

    assert entered.is_set()
    assert not thread.is_alive()


def test_read_lease_skips_digest_while_workflow_lease_and_verify_hash(
    tmp_path: Path,
    monkeypatch,
) -> None:
    catalog = MaterialCatalog(tmp_path / "library")
    record = catalog.add(
        _source(tmp_path / "source.mp4", b"video" * 1024),
        "video",
        "Film",
    )
    material_id = record.material.material_id
    original_fingerprint_file = material_catalog_module.fingerprint_file
    hashed_paths: list[Path] = []

    def tracked_fingerprint_file(path: Path | str, **kwargs) -> str:
        hashed_paths.append(Path(path))
        return original_fingerprint_file(path, **kwargs)

    monkeypatch.setattr(
        material_catalog_module,
        "fingerprint_file",
        tracked_fingerprint_file,
    )

    with catalog.read_lease(material_id) as read_binding:
        assert read_binding.source_path.is_file()
    assert hashed_paths == []

    with catalog.lease(material_id) as workflow_binding:
        assert workflow_binding.source_path == read_binding.source_path
    assert hashed_paths == [workflow_binding.source_path]

    hashed_paths.clear()
    assert catalog.verify(material_id) is True
    assert hashed_paths == [workflow_binding.source_path]


def test_read_lease_is_not_publication_capable_and_rejects_missing_source(
    tmp_path: Path,
) -> None:
    catalog = MaterialCatalog(tmp_path / "library")
    record = catalog.add(
        _source(tmp_path / "source.mp4", b"video"),
        "video",
        "Film",
    )
    staged = tmp_path / "analysis-result.json"
    staged.write_text(json.dumps(_analysis_result(record)), encoding="utf-8")

    with catalog.read_lease(record.material.material_id) as binding:
        with pytest.raises(MaterialLeaseRequiredError, match="active Material lease"):
            catalog.publish_analysis_result(binding, staged)
        managed_source = binding.source_path

    managed_source.unlink()
    with pytest.raises(MaterialInconsistentError, match="source is unavailable"):
        with catalog.read_lease(record.material.material_id):
            pass


def test_read_lease_allows_preview_but_workflow_rejects_changed_bytes(
    tmp_path: Path,
) -> None:
    catalog = MaterialCatalog(tmp_path / "library")
    record = catalog.add(
        _source(tmp_path / "source.mp4", b"123456"),
        "video",
        "Film",
    )
    with catalog.read_lease(record.material.material_id) as binding:
        managed_source = binding.source_path

    managed_source.chmod(stat.S_IRUSR | stat.S_IWUSR)
    managed_source.write_bytes(b"abcdef")

    with catalog.read_lease(record.material.material_id) as binding:
        assert binding.source_path.read_bytes() == b"abcdef"
    assert catalog.verify(record.material.material_id) is False
    with pytest.raises(MaterialInconsistentError, match="is inconsistent"):
        with catalog.lease(record.material.material_id):
            pass


@pytest.mark.parametrize(
    ("material_type", "source_name", "result_name"),
    [
        ("video", "source.mp4", "analysis_result.json"),
        ("music", "source.mp3", "music_analysis_result.json"),
    ],
)
def test_publish_analysis_result_atomically_marks_exact_material_ready(
    tmp_path: Path,
    material_type: str,
    source_name: str,
    result_name: str,
) -> None:
    catalog = MaterialCatalog(tmp_path / "library")
    record = catalog.add(
        _source(tmp_path / source_name, b"source bytes"),
        material_type,
        "Source",
    )
    staged = tmp_path / "stage-result.json"
    payload = json.dumps(_analysis_result(record), ensure_ascii=False).encode()
    staged.write_bytes(payload)

    with catalog.lease(record.material.material_id) as binding:
        published = catalog.publish_analysis_result(binding, staged)
        canonical = binding.memory_root / result_name
        assert canonical.read_bytes() == payload

    assert staged.read_bytes() == payload
    assert published.material.condition is MaterialCondition.READY
    assert (
        catalog.get(record.material.material_id).material.condition
        is MaterialCondition.READY
    )
    assert catalog.validate_analysis_result(record.material.material_id) is True
    assert not list(canonical.parent.glob(f".{result_name}.*.tmp"))


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("schema_version", "1.0"),
        ("status", "failed"),
        ("material_id", "mat_00000000-0000-4000-8000-000000000001"),
        ("material_type", "music"),
        ("material_name", "Another Material"),
        ("material_fingerprint", "0" * 64),
    ],
)
def test_publish_rejects_non_v2_or_mismatched_material_identity(
    tmp_path: Path,
    field_name: str,
    invalid_value: str,
) -> None:
    catalog = MaterialCatalog(tmp_path / "library")
    record = catalog.add(
        _source(tmp_path / "source.mp4", b"video"),
        "video",
        "Film",
    )
    value = _analysis_result(record)
    value[field_name] = invalid_value
    staged = tmp_path / "invalid-result.json"
    staged.write_text(json.dumps(value), encoding="utf-8")

    with catalog.lease(record.material.material_id) as binding:
        with pytest.raises(InvalidAnalysisResultError):
            catalog.publish_analysis_result(binding, staged)
        canonical = binding.memory_root / "analysis_result.json"
        assert not canonical.exists()

    current = catalog.get(record.material.material_id)
    assert current.material.condition is MaterialCondition.QUEUED
    assert catalog.validate_analysis_result(record.material.material_id) is False


def test_ready_requires_valid_canonical_json_not_merely_an_existing_file(
    tmp_path: Path,
) -> None:
    catalog = MaterialCatalog(tmp_path / "library")
    record = catalog.add(
        _source(tmp_path / "source.mp4", b"video"),
        "video",
        "Film",
    )
    binding = _binding(catalog, record)
    canonical = binding.memory_root / "analysis_result.json"

    canonical.write_text('{"schema_version": "2.0",', encoding="utf-8")
    assert catalog.validate_analysis_result(record.material.material_id) is False
    assert (
        catalog.find_by_name("video", "Film").material.condition
        is MaterialCondition.QUEUED
    )

    invalid_identity = _analysis_result(record)
    invalid_identity["material_fingerprint"] = "f" * 64
    canonical.write_text(json.dumps(invalid_identity), encoding="utf-8")
    reopened = MaterialCatalog(catalog.root)
    assert reopened.list()[0].material.condition is MaterialCondition.QUEUED


def test_publish_requires_the_origin_catalogs_current_active_lease(
    tmp_path: Path,
) -> None:
    catalog = MaterialCatalog(tmp_path / "library")
    other_catalog = MaterialCatalog(tmp_path / "other-library")
    record = catalog.add(
        _source(tmp_path / "source.mp3", b"music"),
        "music",
        "Score",
    )
    staged = tmp_path / "result.json"
    staged.write_text(json.dumps(_analysis_result(record)), encoding="utf-8")

    with catalog.lease(record.material.material_id) as binding:
        with pytest.raises(MaterialLeaseRequiredError):
            other_catalog.publish_analysis_result(binding, staged)

    with pytest.raises(MaterialLeaseRequiredError):
        catalog.publish_analysis_result(binding, staged)
    assert catalog.validate_analysis_result(record.material.material_id) is False


def test_publish_rejects_a_symlinked_stage_result(tmp_path: Path) -> None:
    catalog = MaterialCatalog(tmp_path / "library")
    record = catalog.add(
        _source(tmp_path / "source.mp4", b"video"),
        "video",
        "Film",
    )
    target = tmp_path / "target.json"
    target.write_text(json.dumps(_analysis_result(record)), encoding="utf-8")
    staged = tmp_path / "stage.json"
    staged.symlink_to(target)

    with catalog.lease(record.material.material_id) as binding:
        with pytest.raises(InvalidAnalysisResultError):
            catalog.publish_analysis_result(binding, staged)


def test_publish_is_safe_when_stage_is_the_canonical_destination(
    tmp_path: Path,
) -> None:
    catalog = MaterialCatalog(tmp_path / "library")
    record = catalog.add(
        _source(tmp_path / "source.mp4", b"video"),
        "video",
        "Film",
    )
    staged = tmp_path / "result.json"
    payload = json.dumps(_analysis_result(record)).encode()
    staged.write_bytes(payload)

    with catalog.lease(record.material.material_id) as binding:
        catalog.publish_analysis_result(binding, staged)
        canonical = binding.memory_root / "analysis_result.json"

    with catalog.lease(record.material.material_id) as binding:
        published = catalog.publish_analysis_result(binding, canonical)

    assert canonical.read_bytes() == payload
    assert published.material.condition is MaterialCondition.READY


def test_failed_atomic_replace_preserves_the_previous_ready_result(
    tmp_path: Path,
    monkeypatch,
) -> None:
    catalog = MaterialCatalog(tmp_path / "library")
    record = catalog.add(
        _source(tmp_path / "source.mp4", b"video"),
        "video",
        "Film",
    )
    first = tmp_path / "first.json"
    first_payload = json.dumps(_analysis_result(record)).encode()
    first.write_bytes(first_payload)
    with catalog.lease(record.material.material_id) as binding:
        catalog.publish_analysis_result(binding, first)
        canonical = binding.memory_root / "analysis_result.json"

    second_value = _analysis_result(record)
    second_value["elapsed_sec"] = 2.0
    second = tmp_path / "second.json"
    second.write_text(json.dumps(second_value), encoding="utf-8")
    original_replace = Path.replace

    def fail_result_replace(path: Path, target: Path):
        if path.name.startswith(".analysis_result.json."):
            raise OSError("injected atomic replace failure")
        return original_replace(path, target)

    monkeypatch.setattr(Path, "replace", fail_result_replace)
    with catalog.lease(record.material.material_id) as binding:
        with pytest.raises(OSError, match="injected"):
            catalog.publish_analysis_result(binding, second)

    assert canonical.read_bytes() == first_payload
    assert catalog.validate_analysis_result(record.material.material_id) is True
    assert not list(canonical.parent.glob(".analysis_result.json.*.tmp"))


def test_subtitle_sidecar_is_material_owned_immutable_and_idempotent(
    tmp_path: Path,
) -> None:
    catalog = MaterialCatalog(tmp_path / "library")
    record = catalog.add(
        _source(tmp_path / "source.mp4", b"video"),
        "video",
        "Film",
    )
    subtitle = _source(
        tmp_path / "external.srt",
        b"1\n00:00:00,000 --> 00:00:01,000\nHello\n",
    )
    duplicate = _source(tmp_path / "copy.txt", subtitle.read_bytes())
    subtitle_fingerprint = fingerprint_file(subtitle)

    with catalog.lease(record.material.material_id) as binding:
        managed = catalog.ensure_subtitle(binding, subtitle)
        repeated = catalog.ensure_subtitle(binding, duplicate)
        assert catalog.resolve_subtitle(binding) == managed

    subtitle.write_bytes(b"external file changed")
    assert managed == binding.source_path.parent / "sidecars" / "subtitle.srt"
    assert managed != subtitle
    assert managed.read_bytes() == duplicate.read_bytes()
    assert repeated == managed
    assert stat.S_IMODE(managed.stat().st_mode) == 0o400
    metadata_path = managed.parent / "subtitle.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata == {
        "schema_version": "1.0",
        "file": "subtitle.srt",
        "sha256": subtitle_fingerprint,
    }
    # Sidecar identity remains private Material metadata, not a public catalog
    # selector or part of the caller-facing Material domain object.
    assert subtitle_fingerprint not in catalog.manifest_path.read_text(
        encoding="utf-8"
    )
    assert not hasattr(record.material, "subtitle_fingerprint")

    reopened = MaterialCatalog(catalog.root)
    with reopened.lease(record.material.material_id) as binding:
        assert reopened.resolve_subtitle(binding) == managed


def test_subtitle_sidecar_rejects_replacement_and_late_first_binding(
    tmp_path: Path,
) -> None:
    catalog = MaterialCatalog(tmp_path / "library")
    first = catalog.add(
        _source(tmp_path / "first.mp4", b"first video"),
        "video",
        "First",
    )
    with catalog.lease(first.material.material_id) as binding:
        catalog.ensure_subtitle(
            binding,
            _source(tmp_path / "first.srt", b"first subtitle"),
        )
        with pytest.raises(
            SubtitleSidecarConflictError,
            match="replacement is not supported",
        ):
            catalog.ensure_subtitle(
                binding,
                _source(tmp_path / "different.srt", b"different subtitle"),
            )

    late = catalog.add(
        _source(tmp_path / "late.mp4", b"late video"),
        "video",
        "Late",
    )
    staged = tmp_path / "analysis.json"
    staged.write_text(json.dumps(_analysis_result(late)), encoding="utf-8")
    with catalog.lease(late.material.material_id) as binding:
        catalog.publish_analysis_result(binding, staged)
        with pytest.raises(
            SubtitleSidecarConflictError,
            match="after video analysis is complete",
        ):
            catalog.ensure_subtitle(
                binding,
                _source(tmp_path / "late.srt", b"late subtitle"),
            )
        assert catalog.resolve_subtitle(binding) is None


def test_subtitle_sidecar_requires_active_video_lease_and_regular_source(
    tmp_path: Path,
) -> None:
    catalog = MaterialCatalog(tmp_path / "library")
    video = catalog.add(
        _source(tmp_path / "source.mp4", b"video"),
        "video",
        "Film",
    )
    music = catalog.add(
        _source(tmp_path / "source.mp3", b"music"),
        "music",
        "Score",
    )
    subtitle = _source(tmp_path / "subtitle.srt", b"subtitle")
    symlink = tmp_path / "subtitle-link.srt"
    symlink.symlink_to(subtitle)

    with catalog.lease(video.material.material_id) as video_binding:
        with pytest.raises(InvalidSubtitleSidecarError, match="non-symlink"):
            catalog.ensure_subtitle(video_binding, symlink)
    with pytest.raises(MaterialLeaseRequiredError, match="active Material lease"):
        catalog.ensure_subtitle(video_binding, subtitle)
    with pytest.raises(MaterialLeaseRequiredError, match="active Material lease"):
        catalog.resolve_subtitle(video_binding)

    with catalog.lease(music.material.material_id) as music_binding:
        with pytest.raises(ValueError, match="only valid for video"):
            catalog.ensure_subtitle(music_binding, subtitle)
        with pytest.raises(ValueError, match="only valid for video"):
            catalog.resolve_subtitle(music_binding)


def test_resolve_subtitle_detects_private_sidecar_tampering(tmp_path: Path) -> None:
    catalog = MaterialCatalog(tmp_path / "library")
    record = catalog.add(
        _source(tmp_path / "source.mp4", b"video"),
        "video",
        "Film",
    )
    with catalog.lease(record.material.material_id) as binding:
        managed = catalog.ensure_subtitle(
            binding,
            _source(tmp_path / "subtitle.srt", b"subtitle"),
        )

    managed.chmod(stat.S_IRUSR | stat.S_IWUSR)
    managed.write_bytes(b"tampered")
    with catalog.lease(record.material.material_id) as binding:
        with pytest.raises(MaterialInconsistentError, match="fingerprint"):
            catalog.resolve_subtitle(binding)
