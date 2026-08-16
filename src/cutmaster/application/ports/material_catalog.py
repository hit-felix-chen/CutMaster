"""Material persistence and lease boundaries used by Application services."""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, runtime_checkable

from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import Material, MaterialType


@dataclass(frozen=True)
class MaterialRecord:
    """One catalog result plus operation-local idempotent reuse metadata."""

    material: Material
    reused: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.material, Material):
            raise TypeError("material must be a domain Material")
        if not isinstance(self.reused, bool):
            raise TypeError("reused must be a boolean")


@dataclass(frozen=True)
class MaterialBinding:
    """Deletion-safe runtime paths exposed only while a lease is held.

    The lease that produced a binding defines its verification strength.  A
    Workflow lease verifies the complete source digest; a browsing lease only
    validates that the immutable managed source is still a regular file.
    """

    material: Material
    source_path: Path
    memory_root: Path
    manifest_path: Path

    def __post_init__(self) -> None:
        if not isinstance(self.material, Material):
            raise TypeError("material must be a domain Material")
        for field_name in ("source_path", "memory_root", "manifest_path"):
            value = getattr(self, field_name)
            if not isinstance(value, Path):
                raise TypeError(f"{field_name} must be a pathlib.Path")
            if not value.is_absolute():
                raise ValueError(f"{field_name} must be an absolute path")


@runtime_checkable
class MaterialReferenceChecker(Protocol):
    def references(self, material_id: MaterialId) -> Sequence[str]:
        """Return stable descriptions of records that still own this Material."""


@runtime_checkable
class MaterialCatalog(Protocol):
    def add(
        self,
        source: Path | str,
        material_type: MaterialType | str,
        name: str | None = None,
    ) -> MaterialRecord:
        """Strictly import a new immutable Material."""

    def ensure(
        self,
        source: Path | str,
        material_type: MaterialType | str,
        name: str | None = None,
    ) -> MaterialRecord:
        """Reuse only an exact type/name/fingerprint binding, otherwise import."""

    def get(self, material_id: MaterialId) -> MaterialRecord | None:
        """Return one Material by canonical identity."""

    def find_by_name(
        self,
        material_type: MaterialType | str,
        name: str,
    ) -> MaterialRecord | None:
        """Resolve the exact public name within one Material type."""

    def list(
        self,
        material_type: MaterialType | str | None = None,
    ) -> list[MaterialRecord]:
        """List Materials, optionally restricted to one type."""

    def verify(self, material_id: MaterialId) -> bool:
        """Check that the immutable managed source still matches its fingerprint."""

    def require_consistent(self, material_id: MaterialId) -> MaterialRecord:
        """Return one Material or raise when its managed source is inconsistent."""

    def lease(
        self,
        material_id: MaterialId,
    ) -> AbstractContextManager[MaterialBinding]:
        """Hold deletion exclusion and yield verified runtime paths."""

    def read_lease(
        self,
        material_id: MaterialId,
    ) -> AbstractContextManager[MaterialBinding]:
        """Hold deletion exclusion for browsing without hashing source bytes."""

    def ensure_subtitle(
        self,
        binding: MaterialBinding,
        source: Path | str,
    ) -> Path:
        """Bind one immutable subtitle sidecar through an active video lease.

        Repeating the operation with identical bytes is idempotent.  This
        operation never replaces an existing sidecar.
        """

    def resolve_subtitle(self, binding: MaterialBinding) -> Path | None:
        """Resolve and verify a video Material's private subtitle sidecar."""

    def publish_analysis_result(
        self,
        binding: MaterialBinding,
        staged_result_path: Path | str,
    ) -> MaterialRecord:
        """Atomically publish a validated v2 result through an active lease."""

    def validate_analysis_result(self, material_id: MaterialId) -> bool:
        """Validate the canonical result bound to one leased Material."""

    def delete(self, material_id: MaterialId) -> None:
        """Delete a Material only when the configured checker finds no references."""


__all__ = [
    "MaterialBinding",
    "MaterialCatalog",
    "MaterialRecord",
    "MaterialReferenceChecker",
]
