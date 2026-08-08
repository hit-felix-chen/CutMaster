"""Contracts exchanged by the CLI and the three-stage orchestrator."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from cutmaster.contracts.renderer import AudioMode


@dataclass(frozen=True)
class WorkflowRequest:
    video_path: Path
    audio_path: Path
    prompt: str
    output_dir: Path
    target_output_length_sec: float = 60.0
    target_shot_length_sec: float = 4.0
    prompt_type: str = "event"
    video_title: str = ""
    subtitle_path: Path | None = None
    max_clip_duration_sec: float | None = None
    audio_mode: AudioMode = "dialogue"
    overwrite: bool = False


@dataclass(frozen=True)
class WorkflowResult:
    status: str
    analysis_result: str
    planning_result: str
    render_result: str
    render_plan: str
    output_video: str
    material_directory: str
    target_output_length_sec: float
    actual_output_length_sec: float
    num_raw_clips: int
    num_planned_clips: int
    dialogue_audio_included: bool
    stage_timings_sec: dict[str, float]
    wall_clock_sec: float
    schema_version: str = "1.0"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["WorkflowRequest", "WorkflowResult"]
