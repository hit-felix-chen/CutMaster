"""Public service for the reusable material-analysis stage."""

from __future__ import annotations

import shutil
import time

from cutmaster.analyser.material_analyst import MaterialAnalystAgent
from cutmaster.configuration.schema import AppConfig
from cutmaster.contracts.analyser import AnalysisRequest, AnalysisResult
from cutmaster.runtime.observability import log_event


class Analyser:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def analyse(self, request: AnalysisRequest) -> AnalysisResult:
        if not request.video_path.is_file():
            raise FileNotFoundError(f"Video not found: {request.video_path}")
        if request.subtitle_path is not None and not request.subtitle_path.is_file():
            raise FileNotFoundError(f"Subtitle not found: {request.subtitle_path}")
        output_dir = request.output_dir.resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        log_event(
            "INFO",
            "analyser",
            "stage.start",
            "Video material analysis started",
            stage="video_material_analysis",
            source_video=request.video_path.name,
        )
        material = MaterialAnalystAgent(self.config).analyse(
            request.video_path.resolve(),
            request.video_title or request.video_path.stem,
            request.subtitle_path.resolve() if request.subtitle_path else None,
        )
        source_srt = output_dir / "source.srt"
        processed_subtitle = output_dir / "dialogue_merged.srt"
        dialogues_json = output_dir / "dialogues.json"
        shutil.copy2(material.source_srt, source_srt)
        shutil.copy2(material.processed_subtitle, processed_subtitle)
        shutil.copy2(material.dialogues_json, dialogues_json)
        elapsed = time.monotonic() - started
        result = AnalysisResult(
            status="success",
            source_video=str(request.video_path.resolve()),
            material_directory=str(material.material_directory.resolve()),
            source_srt=str(source_srt.resolve()),
            processed_subtitle=str(processed_subtitle.resolve()),
            dialogues_json=str(dialogues_json.resolve()),
            video_description=str(material.video_description_path.resolve()),
            video_summary=str(material.video_summary_path.resolve()),
            analysis_history=str(material.analysis_history_path.resolve()),
            elapsed_sec=elapsed,
            model_usage=(
                str(material.model_usage_path.resolve())
                if material.model_usage_path is not None
                else None
            ),
            model_usage_summary=material.model_usage_summary,
        )
        result.write(output_dir / "analysis_result.json")
        log_event(
            "INFO",
            "analyser",
            "stage.complete",
            "Video material analysis completed",
            stage="video_material_analysis",
            elapsed_sec=elapsed,
            material_directory=material.material_directory,
        )
        return result


__all__ = ["Analyser"]
