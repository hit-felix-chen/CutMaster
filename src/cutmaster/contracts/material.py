"""Data returned by reusable source-material analysis."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MaterialAnalysisResult:
    material_directory: Path
    source_srt: Path
    processed_subtitle: Path
    dialogues_json: Path
    video_description_path: Path
    video_summary_path: Path
    analysis_history_path: Path
    video_description: dict[str, Any]
    video_summary: dict[str, Any]
