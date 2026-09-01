"""Transport-neutral ASTER Run and Frozen Edit commands."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite
from typing import Any

from cutmaster.domain.ids import AttemptId, FrozenEditId, ProjectId, RunId


@dataclass(frozen=True)
class CreateRunCommand:
    command_id: str
    project_id: ProjectId
    target_shot_length_sec: float = 4.0
    prompt_type: str = "event"
    video_title: str = ""
    max_clip_duration_sec: float | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.target_shot_length_sec, "target_shot_length_sec"),
            (self.max_clip_duration_sec, "max_clip_duration_sec"),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(f"{name} must be a number")
            if not isfinite(float(value)) or float(value) <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not isinstance(self.prompt_type, str) or not self.prompt_type.strip():
            raise ValueError("prompt_type must not be empty")
        if not isinstance(self.video_title, str):
            raise TypeError("video_title must be a string")


@dataclass(frozen=True)
class CompleteRunCommand:
    command_id: str
    run_id: RunId
    attempt_id: AttemptId
    plan_relative_path: str
    model_usage_summary: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class CreateRevisionCommand:
    command_id: str
    source_edit_id: FrozenEditId
    plan_relative_path: str


@dataclass(frozen=True)
class TrajectoryReplacement:
    group_id: str
    trajectory_id: str


@dataclass(frozen=True)
class SaveGuidedRevisionCommand:
    command_id: str
    source_edit_id: FrozenEditId
    replacements: tuple[TrajectoryReplacement, ...]


@dataclass(frozen=True)
class RecoverRunCommand:
    command_id: str
    run_id: RunId


@dataclass(frozen=True)
class RunAgainCommand:
    command_id: str
    source_run_id: RunId


@dataclass(frozen=True)
class DeleteRunCommand:
    command_id: str
    run_id: RunId


__all__ = [
    "CompleteRunCommand",
    "CreateRevisionCommand",
    "CreateRunCommand",
    "DeleteRunCommand",
    "RecoverRunCommand",
    "RunAgainCommand",
    "SaveGuidedRevisionCommand",
    "TrajectoryReplacement",
]
