"""Runtime-only Material bindings issued by the Application Layer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialFingerprint, MaterialType


def _validate_runtime_path(path: Path, field_name: str) -> None:
    if not isinstance(path, Path):
        raise TypeError(f"{field_name} must be a pathlib.Path")
    if not path.is_absolute():
        raise ValueError(f"{field_name} must be an absolute runtime path")
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in str(path)
    ):
        raise ValueError(f"{field_name} must not contain control characters")


def _validate_memory_path(
    path: Path,
    material: MaterialRuntimeHandle,
    field_name: str,
) -> None:
    _validate_runtime_path(path, field_name)
    try:
        path.relative_to(material.memory_root)
    except ValueError as exc:
        raise ValueError(
            f"{field_name} must be inside the Material Memory root"
        ) from exc


@dataclass(frozen=True)
class MaterialRuntimeHandle:
    """Verified local access to one leased Material.

    Construction performs lexical validation only. The Application is
    responsible for resolving the paths, verifying the fingerprint, and
    retaining the owning Material lease for the complete stage invocation.
    """

    material_id: MaterialId
    material_type: MaterialType
    material_name: str
    expected_fingerprint: MaterialFingerprint
    source_path: Path
    memory_root: Path
    subtitle_path: Path | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.material_id, MaterialId):
            raise TypeError("material_id must be a MaterialId")
        if not isinstance(self.material_type, MaterialType):
            raise TypeError("material_type must be a MaterialType")
        if not isinstance(self.material_name, str):
            raise TypeError("Material Name must be a string")
        if not self.material_name.strip():
            raise ValueError("Material Name must not be empty")
        if not isinstance(self.expected_fingerprint, MaterialFingerprint):
            raise TypeError("expected_fingerprint must be a MaterialFingerprint")
        _validate_runtime_path(self.source_path, "source_path")
        _validate_runtime_path(self.memory_root, "memory_root")
        if self.subtitle_path is not None:
            if self.material_type is not MaterialType.VIDEO:
                raise ValueError("Only a video Material can own a subtitle sidecar")
            _validate_runtime_path(self.subtitle_path, "subtitle_path")


@dataclass(frozen=True)
class AnalysedVideoRuntimeHandle:
    """A video Material handle bound to its reusable Material Memory."""

    material: MaterialRuntimeHandle
    memory_schema_version: str
    video_description_path: Path
    video_summary_path: Path
    dialogues_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.material, MaterialRuntimeHandle):
            raise TypeError("material must be a MaterialRuntimeHandle")
        if self.material.material_type is not MaterialType.VIDEO:
            raise ValueError("Analysed video handle requires a video Material")
        if not isinstance(self.memory_schema_version, str):
            raise TypeError("Video Material Memory schema version must be a string")
        if not self.memory_schema_version.strip():
            raise ValueError("Video Material Memory schema version must not be empty")
        _validate_memory_path(
            self.video_description_path,
            self.material,
            "video_description_path",
        )
        _validate_memory_path(
            self.video_summary_path,
            self.material,
            "video_summary_path",
        )
        _validate_memory_path(
            self.dialogues_path,
            self.material,
            "dialogues_path",
        )


@dataclass(frozen=True)
class AnalysedMusicRuntimeHandle:
    """A music Material handle bound to its reusable Music Memory."""

    material: MaterialRuntimeHandle
    memory_schema_version: str
    music_memory_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.material, MaterialRuntimeHandle):
            raise TypeError("material must be a MaterialRuntimeHandle")
        if self.material.material_type is not MaterialType.MUSIC:
            raise ValueError("Analysed music handle requires a music Material")
        if not isinstance(self.memory_schema_version, str):
            raise TypeError("Music Memory schema version must be a string")
        if not self.memory_schema_version.strip():
            raise ValueError("Music Memory schema version must not be empty")
        _validate_memory_path(
            self.music_memory_path,
            self.material,
            "music_memory_path",
        )


@dataclass(frozen=True)
class RenderRuntimeBindings:
    """Runtime media bindings supplied separately from a portable RenderPlan."""

    video: MaterialRuntimeHandle
    music: MaterialRuntimeHandle

    def __post_init__(self) -> None:
        if not isinstance(self.video, MaterialRuntimeHandle):
            raise TypeError("video must be a MaterialRuntimeHandle")
        if not isinstance(self.music, MaterialRuntimeHandle):
            raise TypeError("music must be a MaterialRuntimeHandle")
        if self.video.material_type is not MaterialType.VIDEO:
            raise ValueError("Render video binding requires a video Material")
        if self.music.material_type is not MaterialType.MUSIC:
            raise ValueError("Render music binding requires a music Material")


__all__ = [
    "AnalysedMusicRuntimeHandle",
    "AnalysedVideoRuntimeHandle",
    "MaterialRuntimeHandle",
    "RenderRuntimeBindings",
]
