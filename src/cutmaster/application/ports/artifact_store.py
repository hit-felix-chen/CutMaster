"""Resolution boundary for Application-owned artifact references."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from cutmaster.domain.artifacts import ManagedArtifactReference


@runtime_checkable
class ArtifactStore(Protocol):
    def resolve(self, reference: ManagedArtifactReference) -> Path:
        """Resolve a portable reference for an authorized Application use case."""


__all__ = ["ArtifactStore"]
