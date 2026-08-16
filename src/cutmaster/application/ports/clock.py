"""Time source for deterministic Application tests."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current timezone-aware instant."""


__all__ = ["Clock"]
