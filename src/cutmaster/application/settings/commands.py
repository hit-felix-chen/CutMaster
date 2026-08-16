"""Transport-neutral settings commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class SaveSettingsCommand:
    command_id: str
    overlay: Mapping[str, Any]


__all__ = ["SaveSettingsCommand"]

