from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cutmaster.application.ports.artifact_store import ArtifactStore
from cutmaster.application.ports.clock import Clock
from cutmaster.application.ports.data_root import DataRootLocator
from cutmaster.domain import ManagedArtifactReference


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 8, 12, tzinfo=UTC)


class FixedDataRoot:
    def __init__(self, root: Path) -> None:
        self.root = root

    def current(self) -> Path:
        return self.root


class FixedArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def resolve(self, reference: ManagedArtifactReference) -> Path:
        return self.root / reference.relative_path


def test_minimal_application_ports_are_structurally_implementable(
    tmp_path: Path,
) -> None:
    assert isinstance(FixedClock(), Clock)
    assert isinstance(FixedDataRoot(tmp_path), DataRootLocator)
    assert isinstance(FixedArtifactStore(tmp_path), ArtifactStore)
