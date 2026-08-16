"""Portable references to Application-owned artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from unicodedata import category


def validate_portable_relative_file_path(value: str) -> str:
    """Validate one normalized, portable relative file path without I/O."""

    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("Artifact path must be a non-empty POSIX path")
    if any(category(character) == "Cc" for character in value):
        raise ValueError("Artifact path must not contain control characters")
    raw_parts = value.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError(
            "Artifact path must not contain empty, dot, or parent segments"
        )
    path = PurePosixPath(value)
    if path.is_absolute() or value.endswith("/"):
        raise ValueError("Artifact path must identify a relative file")
    return path.as_posix()


@dataclass(frozen=True)
class ManagedArtifactReference:
    owner_id: str
    relative_path: str

    def __post_init__(self) -> None:
        if not isinstance(self.owner_id, str):
            raise TypeError("Artifact owner ID must be a string")
        if not self.owner_id.strip():
            raise ValueError("Artifact owner ID must not be empty")
        normalized = validate_portable_relative_file_path(self.relative_path)
        object.__setattr__(self, "relative_path", normalized)


__all__ = ["ManagedArtifactReference", "validate_portable_relative_file_path"]
