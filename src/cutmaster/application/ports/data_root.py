"""Bootstrap boundary for the active Application Data Root."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class DataRootLocator(Protocol):
    def current(self) -> Path:
        """Return the absolute root selected for this Application context."""


__all__ = ["DataRootLocator"]
