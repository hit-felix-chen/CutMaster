"""Coordinate the independent analyser, planners, and renderer stages."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path

from cutmaster.analyser import Analyser
from cutmaster.configuration.schema import AppConfig
from cutmaster.contracts.analyser import AnalysisRequest, MusicAnalysisRequest
from cutmaster.contracts.planners import PlannersRequest
from cutmaster.contracts.renderer import RenderRequest
from cutmaster.contracts.workflow import WorkflowRequest, WorkflowResult
from cutmaster.planners import Planners
from cutmaster.renderer import Renderer
from cutmaster.runtime.artifact_layout import ArtifactLayout
from cutmaster.runtime.model_gateway import merge_usage_summaries
from cutmaster.runtime.observability import log_event


def _validate_request(request: WorkflowRequest) -> None:
    if not request.video_path.is_file():
        raise FileNotFoundError(f"Video not found: {request.video_path}")
    if not request.audio_path.is_file():
        raise FileNotFoundError(f"BGM not found: {request.audio_path}")
    if request.subtitle_path is not None and not request.subtitle_path.is_file():
        raise FileNotFoundError(f"Subtitle not found: {request.subtitle_path}")
    if not request.prompt.strip():
        raise ValueError("Prompt must not be empty")
    if request.audio_mode not in {"bgm_only", "dialogue"}:
        raise ValueError(f"Unsupported audio mode: {request.audio_mode}")


class Orchestrator:
    """Public facade for a complete three-stage CutMaster workflow."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.analyser = Analyser(config)
        self.planners = Planners(config)
        self.renderer = Renderer(config.renderer)

    def run(self, request: WorkflowRequest) -> WorkflowResult:
        _validate_request(request)
        layout = ArtifactLayout.create(request.output_dir)
        if layout.output_video.exists() and not request.overwrite:
            raise FileExistsError(
                "Rendered output already exists; pass --overwrite: "
                f"{layout.output_video}"
            )
        started = time.monotonic()
        log_event(
            "INFO",
            "orchestrator",
            "workflow.start",
            "CutMaster workflow started",
            source_video=request.video_path.name,
            source_audio=request.audio_path.name,
            audio_mode=request.audio_mode,
            target_duration_sec=request.target_output_length_sec,
            output_dir=layout.root,
        )
        analysis = self.analyser.analyse(
            AnalysisRequest(
                video_path=request.video_path.resolve(),
                output_dir=layout.analyser_dir,
                video_title=request.video_title,
                subtitle_path=(
                    request.subtitle_path.resolve()
                    if request.subtitle_path is not None
                    else None
                ),
                material_name=request.video_material_name,
            )
        )
        music_analysis = self.analyser.analyse_music(
            MusicAnalysisRequest(
                audio_path=request.audio_path.resolve(),
                output_dir=layout.music_analyser_dir,
                material_name=request.music_material_name,
            )
        )
        planners_result = self.planners.plan(
            PlannersRequest(
                video_path=Path(analysis.source_video),
                audio_path=Path(music_analysis.source_audio),
                prompt=request.prompt,
                output_dir=layout.planners_dir,
                target_output_length_sec=request.target_output_length_sec,
                target_shot_length_sec=request.target_shot_length_sec,
                prompt_type=request.prompt_type,
                video_title=request.video_title or analysis.material_name,
                max_clip_duration_sec=request.max_clip_duration_sec,
                overwrite=request.overwrite,
                video_material_name=analysis.material_name,
                music_material_name=music_analysis.material_name,
            ),
            analysis,
            music_analysis,
        )
        rendered = self.renderer.render(
            RenderRequest(
                plan_path=layout.render_plan,
                output_dir=layout.renderer_dir,
                audio_mode=request.audio_mode,
                overwrite=request.overwrite,
            )
        )
        timings = {
            "analyser": analysis.elapsed_sec + music_analysis.elapsed_sec,
            "analyser.video": analysis.elapsed_sec,
            "analyser.music": music_analysis.elapsed_sec,
            "planners": planners_result.wall_clock_sec,
            "renderer": rendered.wall_clock_sec,
            **{
                f"planners.{name}": value
                for name, value in planners_result.stage_timings_sec.items()
            },
            **{
                f"renderer.{name}": value
                for name, value in rendered.stage_timings_sec.items()
            },
        }
        analyser_current = analysis.model_usage_summary
        analyser_cumulative = analysis.model_usage_cumulative_summary
        planners_current = planners_result.model_usage_summary
        planners_cumulative = planners_result.model_usage_cumulative_summary
        current_usage = merge_usage_summaries(
            [analyser_current, planners_current]
        )
        cumulative_usage = merge_usage_summaries(
            [analyser_cumulative, planners_cumulative]
        )
        model_usage = {
            "schema_version": "1.0",
            "currency": "CNY",
            "price_unit": "yuan_per_million_tokens",
            "updated_at": datetime.now().astimezone().isoformat(),
            "current_run": current_usage,
            "cumulative": cumulative_usage,
            "stages": {
                "analyser": {
                    "current_run": analyser_current,
                    "cumulative": analyser_cumulative,
                    "artifact": analysis.model_usage,
                },
                "planners": {
                    "current_run": planners_current,
                    "cumulative": planners_cumulative,
                    "artifact": planners_result.model_usage,
                },
            },
        }
        layout.workflow_model_usage.write_text(
            json.dumps(model_usage, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        result = WorkflowResult(
            status="success",
            analysis_result=str(layout.analysis_result),
            planners_result=str(layout.planners_result),
            render_result=str(layout.render_result),
            render_plan=str(layout.render_plan),
            output_video=rendered.output_video,
            material_directory=analysis.material_directory,
            target_output_length_sec=request.target_output_length_sec,
            actual_output_length_sec=rendered.duration_sec,
            num_raw_clips=planners_result.num_raw_clips,
            num_planned_clips=planners_result.num_planned_clips,
            dialogue_audio_included=request.audio_mode == "dialogue",
            stage_timings_sec=timings,
            wall_clock_sec=time.monotonic() - started,
            model_usage=model_usage,
            model_usage_artifact=str(layout.workflow_model_usage),
            music_analysis_result=str(layout.music_analysis_result),
            video_material_name=analysis.material_name,
            music_material_name=music_analysis.material_name,
            music_material_directory=music_analysis.material_directory,
        )
        layout.workflow_result.write_text(
            json.dumps(result.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        log_event(
            "SUCCESS",
            "orchestrator",
            "workflow.complete",
            "CutMaster workflow completed",
            output_path=rendered.output_video,
            wall_clock_sec=result.wall_clock_sec,
            output_duration_sec=result.actual_output_length_sec,
            clips=result.num_planned_clips,
        )
        return result


__all__ = ["Orchestrator"]
