"""Persistence boundary for the sparse, non-secret local settings overlay."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class SettingsStore(Protocol):
    def read(self) -> Mapping[str, object]:
        """Read the current sparse overlay."""

    def replace(self, values: Mapping[str, object]) -> None:
        """Atomically replace the overlay after Application validation."""


__all__ = ["SettingsStore"]
