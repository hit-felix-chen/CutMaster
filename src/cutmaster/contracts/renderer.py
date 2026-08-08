"""Public contracts for deterministic rendering."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal


AudioMode = Literal["bgm_only", "dialogue"]


@dataclass(frozen=True)
class RenderRequest:
    plan_path: Path
    output_dir: Path
    audio_mode: AudioMode = "dialogue"
    overwrite: bool = False

    def validate(self) -> None:
        if not self.plan_path.is_file():
            raise FileNotFoundError(f"Render plan not found: {self.plan_path}")
        if self.audio_mode not in {"bgm_only", "dialogue"}:
            raise ValueError(f"Unsupported audio mode: {self.audio_mode}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_path": str(self.plan_path.resolve()),
            "output_dir": str(self.output_dir.resolve()),
            "audio_mode": self.audio_mode,
            "overwrite": self.overwrite,
        }


@dataclass(frozen=True)
class RenderResult:
    status: str
    plan_id: str
    render_id: str
    audio_mode: AudioMode
    output_video: str
    montage_video: str
    duration_sec: float
    frames: int
    montage_reused: bool
    dialogue_audio_reused: bool
    stage_timings_sec: dict[str, float]
    wall_clock_sec: float
    schema_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path

    @classmethod
    def read(cls, path: Path) -> "RenderResult":
        return cls(**json.loads(path.read_text(encoding="utf-8")))


__all__ = ["AudioMode", "RenderRequest", "RenderResult"]
