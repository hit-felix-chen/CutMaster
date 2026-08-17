"""Caller-safe Material Library read models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialCondition, MaterialType


@dataclass(frozen=True)
class MaterialView:
    """Material identity without fingerprints or managed filesystem paths."""

    material_id: MaterialId
    material_type: MaterialType
    name: str
    condition: MaterialCondition
    reused: bool = False


@dataclass(frozen=True)
class MaterialDetailView:
    """Lightweight Material drawer projection."""

    material: MaterialView
    references: tuple[str, ...]
    analysis_available: bool
    duration_sec: float
    source: Mapping[str, Any]
    memory_summary: Mapping[str, Any]
    preview_available: bool
    analysis_cost_yuan: float | None

    @property
    def reference_count(self) -> int:
        return len(self.references)


@dataclass(frozen=True)
class MaterialMemoryView:
    """One sanitized, type-specific Material Memory tab."""

    material_id: MaterialId
    material_type: MaterialType
    tab: str
    limit: int
    offset: int
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class MaterialPreviewView:
    """One bounded, path-free card preview projection."""

    material_id: MaterialId
    material_type: MaterialType
    media_type: str
    content: bytes

    def __post_init__(self) -> None:
        if not self.media_type.startswith("image/"):
            raise ValueError("Material previews must use an image media type")
        if not self.content:
            raise ValueError("Material preview content must not be empty")


def frozen_payload(value: Mapping[str, Any]) -> Mapping[str, Any]:
    def freeze(item: Any) -> Any:
        if isinstance(item, Mapping):
            return MappingProxyType(
                {str(key): freeze(child) for key, child in item.items()}
            )
        if isinstance(item, list):
            return tuple(freeze(child) for child in item)
        return item

    result = freeze(value)
    if not isinstance(result, Mapping):
        raise TypeError("Material Memory payload must be a mapping")
    return result


__all__ = [
    "MaterialDetailView",
    "MaterialMemoryView",
    "MaterialPreviewView",
    "MaterialView",
    "frozen_payload",
]
