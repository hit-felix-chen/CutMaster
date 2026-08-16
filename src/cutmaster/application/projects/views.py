"""Read models returned by Edit Project use cases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from cutmaster.domain.ids import MaterialId, ProjectId
from cutmaster.domain.projects import CreativeBrief


@dataclass(frozen=True)
class ProjectView:
    project_id: ProjectId
    name: str
    video_material_ids: tuple[MaterialId, ...]
    music_material_ids: tuple[MaterialId, ...]
    creative_brief: CreativeBrief | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class DeletedProjectView:
    project_id: ProjectId
    deleted: bool


__all__ = ["DeletedProjectView", "ProjectView"]

