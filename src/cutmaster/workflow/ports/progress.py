"""Progress reporting boundary for bounded Workflow work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ProgressUpdate:
    completed: int
    total: int | None
    description: str
    unit: str

    def __post_init__(self) -> None:
        if self.completed < 0:
            raise ValueError("Completed progress must not be negative")
        if self.total is not None and (
            self.total < 0 or self.completed > self.total
        ):
            raise ValueError("Progress must not exceed its non-negative total")
        if not self.description.strip() or not self.unit.strip():
            raise ValueError("Progress description and unit must not be empty")


@runtime_checkable
class ProgressReporter(Protocol):
    def report(self, update: ProgressUpdate) -> None:
        """Publish the latest progress snapshot."""


__all__ = ["ProgressReporter", "ProgressUpdate"]
