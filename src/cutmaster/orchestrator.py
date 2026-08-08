"""Coordinate the independent analyser, planners, and renderer stages."""

from __future__ import annotations

import json
import time

from cutmaster.analyser import Analyser
from cutmaster.configuration.schema import AppConfig
from cutmaster.contracts.analyser import AnalysisRequest
from cutmaster.contracts.planning import PlanningRequest
from cutmaster.contracts.renderer import RenderRequest
from cutmaster.contracts.workflow import WorkflowRequest, WorkflowResult
from cutmaster.planners import Planner
from cutmaster.renderer import Renderer
from cutmaster.runtime.artifact_layout import ArtifactLayout
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
        self.planner = Planner(config)
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
            )
        )
        planning = self.planner.plan(
            PlanningRequest(
                video_path=request.video_path.resolve(),
                audio_path=request.audio_path.resolve(),
                prompt=request.prompt,
                output_dir=layout.planners_dir,
                target_output_length_sec=request.target_output_length_sec,
                target_shot_length_sec=request.target_shot_length_sec,
                prompt_type=request.prompt_type,
                video_title=request.video_title,
                max_clip_duration_sec=request.max_clip_duration_sec,
                overwrite=request.overwrite,
            ),
            analysis,
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
            "analyser": analysis.elapsed_sec,
            "planners": planning.wall_clock_sec,
            "renderer": rendered.wall_clock_sec,
            **{
                f"planners.{name}": value
                for name, value in planning.stage_timings_sec.items()
            },
            **{
                f"renderer.{name}": value
                for name, value in rendered.stage_timings_sec.items()
            },
        }
        result = WorkflowResult(
            status="success",
            analysis_result=str(layout.analysis_result),
            planning_result=str(layout.planning_result),
            render_result=str(layout.render_result),
            render_plan=str(layout.render_plan),
            output_video=rendered.output_video,
            material_directory=analysis.material_directory,
            target_output_length_sec=request.target_output_length_sec,
            actual_output_length_sec=rendered.duration_sec,
            num_raw_clips=planning.num_raw_clips,
            num_planned_clips=planning.num_planned_clips,
            dialogue_audio_included=request.audio_mode == "dialogue",
            stage_timings_sec=timings,
            wall_clock_sec=time.monotonic() - started,
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
