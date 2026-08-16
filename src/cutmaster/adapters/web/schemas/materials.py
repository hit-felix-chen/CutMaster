"""Material command request bodies."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from cutmaster.domain.materials import MaterialType


class MaterialPreflightBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    material_type: MaterialType
    name: str


__all__ = ["MaterialPreflightBody"]
