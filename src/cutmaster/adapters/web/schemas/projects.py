"""Project command request bodies."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class WebRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class CreateProjectBody(WebRequest):
    name: str = "Untitled Project"


class RenameProjectBody(WebRequest):
    name: str


class SetProjectMaterialsBody(WebRequest):
    video_material_ids: list[str] = Field(default_factory=list, max_length=1)
    music_material_ids: list[str] = Field(default_factory=list, max_length=1)


class SaveCreativeBriefBody(WebRequest):
    editing_intent: str
    target_duration_sec: float = Field(gt=0)


class SaveProjectSetupBody(WebRequest):
    video_material_ids: list[str] = Field(default_factory=list, max_length=1)
    music_material_ids: list[str] = Field(default_factory=list, max_length=1)
    editing_intent: str
    target_duration_sec: float = Field(gt=0)


__all__ = [
    "CreateProjectBody",
    "RenameProjectBody",
    "SaveCreativeBriefBody",
    "SaveProjectSetupBody",
    "SetProjectMaterialsBody",
]
