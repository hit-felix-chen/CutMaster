"""Public commands and receipts for the managed CutMaster workflow."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import isfinite
from pathlib import Path
from typing import Any, Literal


AudioMode = Literal["bgm_only", "dialogue"]


def _positive(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    normalized = float(value)
    if not isfinite(normalized) or normalized <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return normalized


@dataclass(frozen=True, kw_only=True)
class ExecuteManagedWorkflowCommand:
    """Create one managed Project, ASTER Run, Frozen Edit, and Render Variant."""

    prompt: str
    video_path: Path | None = None
    audio_path: Path | None = None
    video_material: str = ""
    music_material: str = ""
    project_name: str = "CutMaster CLI"
    target_output_length_sec: float = 60.0
    anchor_enabled: bool = True
    target_shot_length_sec: float = 4.0
    prompt_type: str = "event"
    video_title: str = ""
    subtitle_path: Path | None = None
    max_clip_duration_sec: float | None = None
    audio_mode: AudioMode = "dialogue"
    video_material_name: str = ""
    music_material_name: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.anchor_enabled, bool):
            raise TypeError("anchor_enabled must be a boolean")
        if (self.video_path is None) == (not self.video_material):
            raise ValueError(
                "Exactly one of video_path or video_material must be supplied"
            )
        if (self.audio_path is None) == (not self.music_material):
            raise ValueError(
                "Exactly one of audio_path or music_material must be supplied"
            )
        for field_name in ("video_path", "audio_path", "subtitle_path"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, Path):
                raise TypeError(f"{field_name} must be a pathlib.Path or None")
        for value, label in (
            (self.prompt, "Prompt"),
            (self.project_name, "Project Name"),
            (self.prompt_type, "Prompt type"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must not be empty")
        object.__setattr__(
            self,
            "target_output_length_sec",
            _positive(self.target_output_length_sec, "Target output duration"),
        )
        object.__setattr__(
            self,
            "target_shot_length_sec",
            _positive(self.target_shot_length_sec, "Target shot duration"),
        )
        if self.max_clip_duration_sec is not None:
            object.__setattr__(
                self,
                "max_clip_duration_sec",
                _positive(self.max_clip_duration_sec, "Maximum clip duration"),
            )
        if self.audio_mode not in {"bgm_only", "dialogue"}:
            raise ValueError(f"Unsupported audio mode: {self.audio_mode!r}")
        if self.video_material and self.video_material_name:
            raise ValueError("video_material_name is only valid with video_path")
        if self.music_material and self.music_material_name:
            raise ValueError("music_material_name is only valid with audio_path")
        if self.video_material and self.subtitle_path is not None:
            raise ValueError(
                "subtitle_path cannot replace analysis for an existing Material"
            )


@dataclass(frozen=True)
class ManagedWorkflowResult:
    """Portable receipt for a completed managed workflow."""

    status: str
    project_id: str
    run_id: str
    frozen_edit_id: str
    render_variant_id: str
    video_material_id: str
    music_material_id: str
    video_material_name: str
    music_material_name: str
    target_output_length_sec: float
    actual_output_length_sec: float
    dialogue_audio_included: bool
    artifact_root: str
    artifacts: dict[str, str] = field(default_factory=dict)
    artifact_manifest_version: str = "1.0"
    model_usage: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "2.0"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["AudioMode", "ExecuteManagedWorkflowCommand", "ManagedWorkflowResult"]
