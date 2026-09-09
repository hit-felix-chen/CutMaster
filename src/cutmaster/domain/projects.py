"""Edit Project values; managed persistence arrives after phase 1."""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite

from cutmaster.domain.ids import ProjectId


@dataclass(frozen=True)
class CreativeBrief:
    editing_intent: str
    target_duration_sec: float
    anchor_enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.anchor_enabled, bool):
            raise TypeError("anchor_enabled must be a boolean")
        if not isinstance(self.editing_intent, str):
            raise TypeError("Editing Intent must be a string")
        if not self.editing_intent.strip():
            raise ValueError("Editing Intent must not be empty")
        if isinstance(self.target_duration_sec, bool) or not isinstance(
            self.target_duration_sec,
            (int, float),
        ):
            raise TypeError("Target Duration must be a number")
        try:
            duration = float(self.target_duration_sec)
        except OverflowError as exc:
            raise ValueError("Target Duration must be finite and positive") from exc
        if not isfinite(duration) or duration <= 0:
            raise ValueError("Target Duration must be finite and positive")
        object.__setattr__(self, "target_duration_sec", duration)


@dataclass(frozen=True)
class EditProject:
    project_id: ProjectId
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, ProjectId):
            raise TypeError("project_id must be a ProjectId")
        if not isinstance(self.name, str):
            raise TypeError("Project Name must be a string")
        if not self.name.strip():
            raise ValueError("Project Name must not be empty")


__all__ = ["CreativeBrief", "EditProject"]
