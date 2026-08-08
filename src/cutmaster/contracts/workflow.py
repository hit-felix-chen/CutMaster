"""Contracts exchanged by the CLI and workflow orchestrator."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RunRequest:
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
    include_dialogue_audio: bool = True
    overwrite: bool = False


@dataclass(frozen=True)
class OrchestrationResult:
    status: str
    output_video: str
    source_srt: str
    processed_subtitle: str
    dialogues_json: str
    music_profile: str
    material_directory: str
    video_description: str
    video_summary: str
    analysis_history: str
    planning_history: str
    planning_calls: str
    edit_plan: str
    candidate_pool: str
    dialogue_anchors: str
    raw_script: str
    adapted_script: str
    montage_video: str
    target_output_length_sec: float
    actual_output_length_sec: float
    raw_script_duration_sec: float
    adapted_script_duration_sec: float
    num_raw_clips: int
    num_adapted_clips: int
    stage_timings_sec: dict[str, float]
    wall_clock_sec: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
