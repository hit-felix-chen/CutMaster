"""Commands for synchronous component-level direct execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cutmaster.contracts.workflow import AudioMode
from cutmaster.workflow.ports import ProgressReporter


@dataclass(frozen=True, kw_only=True)
class AnalyseVideoCommand:
    video_path: Path
    output_dir: Path | None = None
    video_title: str = ""
    subtitle_path: Path | None = None
    material_name: str = ""


@dataclass(frozen=True, kw_only=True)
class AnalyseMusicCommand:
    audio_path: Path
    output_dir: Path | None = None
    material_name: str = ""


@dataclass(frozen=True, kw_only=True)
class PlanCommand:
    prompt: str
    output_dir: Path | None = None
    analysis_result_path: Path | None = None
    video_material: str = ""
    audio_path: Path | None = None
    music_analysis_result_path: Path | None = None
    music_material: str = ""
    music_material_name: str = ""
    target_output_length_sec: float = 60.0
    target_shot_length_sec: float = 4.0
    prompt_type: str = "event"
    max_clip_duration_sec: float | None = None
    overwrite: bool = False
    progress_reporter: ProgressReporter | None = None


@dataclass(frozen=True, kw_only=True)
class RenderCommand:
    plan_path: Path
    output_dir: Path | None = None
    audio_mode: AudioMode = "dialogue"
    overwrite: bool = False


__all__ = [
    "AnalyseMusicCommand",
    "AnalyseVideoCommand",
    "PlanCommand",
    "RenderCommand",
]
