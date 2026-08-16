"""Transport-neutral ASTER Run and Frozen Edit commands."""

from __future__ import annotations

from dataclasses import dataclass

from cutmaster.domain.ids import AttemptId, FrozenEditId, ProjectId, RunId


@dataclass(frozen=True)
class CreateRunCommand:
    command_id: str
    project_id: ProjectId


@dataclass(frozen=True)
class CompleteRunCommand:
    command_id: str
    run_id: RunId
    attempt_id: AttemptId
    plan_relative_path: str


@dataclass(frozen=True)
class CreateRevisionCommand:
    command_id: str
    source_edit_id: FrozenEditId
    plan_relative_path: str


@dataclass(frozen=True)
class RecoverRunCommand:
    command_id: str
    run_id: RunId


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
]

