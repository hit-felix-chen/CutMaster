import hashlib
import json
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Thread

import pytest

from cutmaster.analyser.tools.material_library import (
    InvalidMaterialNameError,
    MaterialInconsistentError,
    MaterialLibrary,
    MaterialNotFoundError,
    MaterialReferencedError,
    MaterialType,
    fingerprint_file,
    normalize_material_name,
)


def _source(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


def test_add_uses_normalized_default_name_and_managed_immutable_copy(
    tmp_path: Path,
) -> None:
    source = _source(tmp_path / "  Opening   Scene  .mp4", b"video bytes")
    library = MaterialLibrary(tmp_path / "library")

    material = library.add(source, MaterialType.VIDEO)
    source.write_bytes(b"changed outside the library")

    assert material.name == "Opening Scene"
    assert material.candidate_name == "Opening Scene"
    assert material.material_type is MaterialType.VIDEO
    assert material.fingerprint == hashlib.sha256(b"video bytes").hexdigest()
    assert len(material.fingerprint) == 64
    assert material.source_path.read_bytes() == b"video bytes"
    assert material.source_path != source
    assert material.source_path.relative_to(library.root).parts[0] == "video"
    assert material.source_path.stat().st_mode & stat.S_IWUSR == 0
    assert material.analysis_dir.is_dir()
    assert material.reused is False
    assert library.verify("video", " Opening   Scene ") is True

    manifest = json.loads(library.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["materials"][0]["name"] == "Opening Scene"
    assert manifest["materials"][0]["fingerprint"] == material.fingerprint
    assert str(source) not in library.manifest_path.read_text(encoding="utf-8")


def test_same_candidate_name_family_and_fingerprint_reuses_material(
    tmp_path: Path,
) -> None:
    first_source = _source(tmp_path / "one.mp4", b"same")
    second_source = _source(tmp_path / "two.mp4", b"same")
    library = MaterialLibrary(tmp_path / "library")

    first = library.add(first_source, "video", name="  Launch\tFilm ")
    second = library.add(second_source, "VIDEO", name="Launch Film")

    assert second.name == first.name == "Launch Film"
    assert second.source_path == first.source_path
    assert second.reused is True
    assert len(library.list()) == 1


def test_same_candidate_name_with_new_bytes_gets_suffix_and_later_reuses_it(
    tmp_path: Path,
) -> None:
    library = MaterialLibrary(tmp_path / "library")
    first = library.add(_source(tmp_path / "a.mp4", b"a"), "video", "Trailer")
    second = library.add(_source(tmp_path / "b.mp4", b"b"), "video", "Trailer")
    third = library.add(_source(tmp_path / "c.mp4", b"c"), "video", "Trailer")
    repeated_second = library.add(
        _source(tmp_path / "another-b.mp4", b"b"), "video", "Trailer"
    )

    assert [first.name, second.name, third.name] == [
        "Trailer",
        "Trailer (2)",
        "Trailer (3)",
    ]
    assert second.candidate_name == third.candidate_name == "Trailer"
    assert repeated_second.name == "Trailer (2)"
    assert repeated_second.reused is True
    assert len(library.list("video")) == 3


def test_allocated_public_name_can_be_passed_back_for_exact_reuse(
    tmp_path: Path,
) -> None:
    library = MaterialLibrary(tmp_path / "library")
    library.add(_source(tmp_path / "a.mp4", b"a"), "video", "Trailer")
    allocated = library.add(
        _source(tmp_path / "b.mp4", b"b"),
        "video",
        "Trailer",
    )

    selected_again = library.add(
        _source(tmp_path / "renamed.mp4", b"b"),
        "video",
        allocated.name,
    )

    assert allocated.name == "Trailer (2)"
    assert selected_again.name == "Trailer (2)"
    assert selected_again.source_path == allocated.source_path
    assert selected_again.reused is True


def test_equal_fingerprint_with_different_candidate_name_or_type_is_distinct(
    tmp_path: Path,
) -> None:
    library = MaterialLibrary(tmp_path / "library")
    source = _source(tmp_path / "media.bin", b"identical")

    video_a = library.add(source, "video", "A")
    video_b = library.add(source, "video", "B")
    music_a = library.add(source, "music", "A")

    assert video_a.fingerprint == video_b.fingerprint == music_a.fingerprint
    assert video_a.source_path != video_b.source_path
    assert video_a.source_path != music_a.source_path
    assert [item.name for item in library.list("video")] == ["A", "B"]
    assert [item.name for item in library.list("music")] == ["A"]


def test_resolve_uses_type_and_normalized_public_name(tmp_path: Path) -> None:
    library = MaterialLibrary(tmp_path / "library")
    source = _source(tmp_path / "source.mov", b"bytes")
    video = library.add(source, "video", "Shared Name")
    music = library.add(source, "music", "Shared Name")

    assert library.resolve("video", " Shared   Name ") == video
    assert library.resolve("music", "Shared Name") == music
    with pytest.raises(MaterialNotFoundError):
        library.resolve("video", "missing")
    with pytest.raises(ValueError, match="Unsupported material type"):
        library.resolve("image", "Shared Name")


def test_verify_detects_changed_managed_source(tmp_path: Path) -> None:
    library = MaterialLibrary(tmp_path / "library")
    source = _source(tmp_path / "source.mp3", b"music")
    material = library.add(source, "music", "Score")
    material.source_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    material.source_path.write_bytes(b"tampered")

    assert library.verify("music", "Score") is False
    with pytest.raises(MaterialInconsistentError):
        library.require_consistent("music", "Score")
    with pytest.raises(MaterialInconsistentError):
        library.add(source, "music", "Score")
    with pytest.raises(MaterialInconsistentError):
        library.add(material.source_path, "music", "Score")
    assert [item.name for item in library.list("music")] == ["Score"]


def test_delete_rejects_referenced_material_then_removes_unreferenced_one(
    tmp_path: Path,
) -> None:
    library = MaterialLibrary(tmp_path / "library")
    material = library.add(
        _source(tmp_path / "source.mp4", b"video"), "video", "Clip"
    )

    with pytest.raises(MaterialReferencedError):
        library.delete("video", "Clip", referenced=True)
    assert material.source_path.exists()

    library.delete("video", "Clip", referenced=False)

    assert not material.source_path.exists()
    assert library.list() == []
    with pytest.raises(MaterialNotFoundError):
        library.resolve("video", "Clip")


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
        library = MaterialLibrary(library_root)
        barrier.wait()
        return library.add(
            sources[index],
            "video",
            f"Material {index}",
        ).name

    with ThreadPoolExecutor(max_workers=len(sources)) as executor:
        returned = list(executor.map(add, range(len(sources))))

    persisted = MaterialLibrary(library_root).list("video")
    assert sorted(returned) == sorted(
        f"Material {index}" for index in range(12)
    )
    assert sorted(material.name for material in persisted) == sorted(returned)
    assert all(material.source_path.is_file() for material in persisted)


def test_analysis_lock_serializes_work_for_one_material(tmp_path: Path) -> None:
    library = MaterialLibrary(tmp_path / "library")
    material = library.add(
        _source(tmp_path / "source.mp4", b"video"),
        "video",
        "Film",
    )
    attempted = Event()
    entered = Event()

    def contender() -> None:
        attempted.set()
        with library.analysis_lock(material):
            entered.set()

    with library.analysis_lock(material):
        thread = Thread(target=contender)
        thread.start()
        assert attempted.wait(timeout=1.0)
        assert not entered.wait(timeout=0.05)
    thread.join(timeout=1.0)

    assert entered.is_set()
    assert not thread.is_alive()
