from __future__ import annotations

from typing import Any


class RetryablePlanningStageError(ValueError):
    """A validated ASTER stage failed and may be redesigned in the next round."""

    def __init__(self, stage: str, diagnostics: dict[str, Any]) -> None:
        diagnosis = str(diagnostics.get("diagnosis") or "planning failed")
        super().__init__(f"{stage} failed: {diagnosis}")
        self.stage = stage
        self.diagnostics = diagnostics


class NoFeasiblePathError(ValueError):
    """Raised when no chronological candidate path can satisfy the edit plan."""

    def __init__(self, slot_id: str, diagnostics: dict[str, Any]) -> None:
        super().__init__(f"No chronological non-overlapping path remains at {slot_id}")
        self.diagnostics = diagnostics


class GroupNoCandidateError(ValueError):
    """Raised when retrieval finds no complete trajectory for a Slot Group."""

    def __init__(self, diagnostics: dict[str, Any]) -> None:
        failed_group_ids = [
            str(value) for value in diagnostics.get("failed_group_ids") or []
        ]
        label = ", ".join(failed_group_ids) or "unknown group"
        super().__init__(f"No complete candidate trajectory remains for {label}")
        self.diagnostics = diagnostics


__all__ = [
    "GroupNoCandidateError",
    "NoFeasiblePathError",
    "RetryablePlanningStageError",
]
