"""Settings command request bodies."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class SaveSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    overlay: dict[str, Any]


__all__ = ["SaveSettingsBody"]
