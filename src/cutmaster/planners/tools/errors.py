from __future__ import annotations

from typing import Any


class NoFeasiblePathError(ValueError):
    """Raised when no chronological candidate path can satisfy the edit plan."""

    def __init__(self, slot_id: str, diagnostics: dict[str, Any]) -> None:
        super().__init__(f"No chronological non-overlapping path remains at {slot_id}")
        self.diagnostics = diagnostics


__all__ = ["NoFeasiblePathError"]

