"""Material identity and lifecycle values."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from cutmaster.domain.ids import MaterialId


class MaterialType(StrEnum):
    VIDEO = "video"
    MUSIC = "music"


class MaterialCondition(StrEnum):
    UPLOADING = "uploading"
    QUEUED = "queued"
    ANALYSING = "analysing"
    READY = "ready"
    FAILED = "failed"
    INCONSISTENT = "inconsistent"


@dataclass(frozen=True, order=True)
class MaterialFingerprint:
    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError("Material fingerprint must be a string")
        if len(self.value) != 64 or any(
            character not in "0123456789abcdef" for character in self.value
        ):
            raise ValueError("Material fingerprint must be a lowercase SHA-256")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Material:
    material_id: MaterialId
    material_type: MaterialType
    name: str
    fingerprint: MaterialFingerprint
    condition: MaterialCondition

    def __post_init__(self) -> None:
        if not isinstance(self.material_id, MaterialId):
            raise TypeError("material_id must be a MaterialId")
        if not isinstance(self.material_type, MaterialType):
            raise TypeError("material_type must be a MaterialType")
        if not isinstance(self.name, str):
            raise TypeError("Material Name must be a string")
        if not self.name.strip():
            raise ValueError("Material Name must not be empty")
        if not isinstance(self.fingerprint, MaterialFingerprint):
            raise TypeError("fingerprint must be a MaterialFingerprint")
        if not isinstance(self.condition, MaterialCondition):
            raise TypeError("condition must be a MaterialCondition")


__all__ = [
    "Material",
    "MaterialCondition",
    "MaterialFingerprint",
    "MaterialType",
]
