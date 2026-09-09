"""Transport-neutral commands for Edit Project use cases."""

from __future__ import annotations

from dataclasses import dataclass

from cutmaster.domain.ids import MaterialId, ProjectId


@dataclass(frozen=True)
class CreateProjectCommand:
    command_id: str
    name: str = "Untitled Project"


@dataclass(frozen=True)
class EnsureProjectByNameCommand:
    command_id: str
    name: str = "Untitled Project"


@dataclass(frozen=True)
class RenameProjectCommand:
    command_id: str
    project_id: ProjectId
    name: str


@dataclass(frozen=True)
class SetProjectMaterialsCommand:
    command_id: str
    project_id: ProjectId
    video_material_ids: tuple[MaterialId, ...] = ()
    music_material_ids: tuple[MaterialId, ...] = ()


@dataclass(frozen=True)
class SaveCreativeBriefCommand:
    command_id: str
    project_id: ProjectId
    editing_intent: str
    target_duration_sec: float
    anchor_enabled: bool = True


@dataclass(frozen=True)
class SaveProjectSetupCommand:
    command_id: str
    project_id: ProjectId
    video_material_ids: tuple[MaterialId, ...]
    music_material_ids: tuple[MaterialId, ...]
    editing_intent: str
    target_duration_sec: float
    anchor_enabled: bool = True


@dataclass(frozen=True)
class DeleteProjectCommand:
    command_id: str
    project_id: ProjectId


__all__ = [
    "CreateProjectCommand",
    "DeleteProjectCommand",
    "EnsureProjectByNameCommand",
    "RenameProjectCommand",
    "SaveCreativeBriefCommand",
    "SaveProjectSetupCommand",
    "SetProjectMaterialsCommand",
]
