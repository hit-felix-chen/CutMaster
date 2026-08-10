"""Public service coordinating the complete ASTER planning stage."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import AppConfig
from cutmaster.contracts.analyser import AnalysisResult, MusicAnalysisResult
from cutmaster.contracts.planning import PlanningRequest, PlanningResult
from cutmaster.planners.aster_team import ASTERTeam
from cutmaster.planners.plan_compiler import (
    compile_render_plan,
    write_script,
)
from cutmaster.planners.tools.errors import NoFeasiblePathError
from cutmaster.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)
from cutmaster.runtime.observability import error_summary, log_event
from cutmaster.runtime.workflow_context import WorkflowContext


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _validate_request(
    request: PlanningRequest,
    analysis: AnalysisResult,
    music_analysis: MusicAnalysisResult,
) -> None:
    analysis.validate()
    music_analysis.validate()
    if request.video_path.resolve() != Path(analysis.source_video).resolve():
        raise ValueError("Planning source video does not match the analysis result")
    if not request.audio_path.is_file():
        raise FileNotFoundError(f"BGM not found: {request.audio_path}")
    if request.audio_path.resolve() != Path(music_analysis.source_audio).resolve():
        raise ValueError("Planning BGM does not match the music analysis result")
    if (
        request.video_material_name
        and request.video_material_name != analysis.material_name
    ):
        raise ValueError("Planning video Material Name does not match the analysis result")
    if (
        request.music_material_name
        and request.music_material_name != music_analysis.material_name
    ):
        raise ValueError("Planning music Material Name does not match the analysis result")
    if not request.prompt.strip():
        raise ValueError("Prompt must not be empty")
    if request.target_output_length_sec <= 0:
        raise ValueError("Target output duration must be positive")
    if request.target_shot_length_sec <= 0:
        raise ValueError("Target shot duration must be positive")
    if request.max_clip_duration_sec is not None and request.max_clip_duration_sec <= 0:
        raise ValueError("Maximum clip duration must be positive")


class Planner:
    """Make all semantic and frame-timing edit decisions."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def plan(
        self,
        request: PlanningRequest,
        analysis: AnalysisResult,
        music_analysis: MusicAnalysisResult,
    ) -> PlanningResult:
        _validate_request(request, analysis, music_analysis)
        output_dir = request.output_dir.resolve()
        diagnostics_dir = output_dir / "diagnostics"
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        render_plan_path = output_dir / "render_plan.json"
        if render_plan_path.exists() and not request.overwrite:
            raise FileExistsError(
                f"Render plan already exists; pass --overwrite: {render_plan_path}"
            )
        planning_history_path = diagnostics_dir / "planning_history.json"
        planning_calls_path = diagnostics_dir / "planning_calls.json"
        model_usage_path = diagnostics_dir / "model_usage.json"
        if request.overwrite:
            for stale_path in (
                planning_history_path,
                planning_calls_path,
                model_usage_path,
            ):
                if stale_path.exists():
                    stale_path.unlink()
        music_profile_path = output_dir / "music_profile.json"
        edit_plan_path = output_dir / "edit_plan.json"
        dialogue_anchors_path = output_dir / "dialogue_anchors.json"
        candidate_pool_path = output_dir / "candidate_pool.json"
        raw_script_path = output_dir / "script_raw.json"
        selection_path = diagnostics_dir / "selection_diagnostics.json"
        result_path = output_dir / "planning_result.json"

        started = time.monotonic()
        timings: dict[str, float] = {}
        video_description = analysis.load_video_description()
        video_summary = analysis.load_video_summary()
        music_memory = music_analysis.load_music_memory()
        context = WorkflowContext(
            planning_history_path,
            model_call_tree_path=planning_calls_path,
            model_usage_path=model_usage_path,
            stage_name="planning",
        )
        context.set_artifact("video_description", video_description)
        context.set_artifact("video_summary", video_summary)
        team = ASTERTeam(request.video_path, self.config, context)

        stage_started = time.monotonic()
        music_profile = team.profile_music(
            music_memory,
            request.target_output_length_sec,
            music_profile_path,
        )
        timings["music_profile"] = time.monotonic() - stage_started

        planning_seconds = 0.0
        anchor_seconds = 0.0
        retrieval_seconds = 0.0
        selection_seconds = 0.0
        slots: list[dict[str, Any]] = []
        candidate_pool: dict[str, list[dict[str, Any]]] = {}
        beam_path: list[dict[str, Any]] = []
        pairwise_scores: dict[str, dict[str, Any]] = {}
        selection: dict[str, Any] = {}
        max_replans = self.config.planners.slot_planning.replan_max_rounds
        for planning_attempt in range(1, max_replans + 2):
            stage_started = time.monotonic()
            log_event(
                "INFO",
                "aster.arrangement",
                "stage.start",
                "Slot planning started",
                stage="slot_planning",
                attempt=planning_attempt,
            )
            slots = team.arrange(request, music_profile)
            context.set_artifact("edit_plan", slots)
            _write_json(edit_plan_path, slots)
            elapsed = time.monotonic() - stage_started
            planning_seconds += elapsed
            log_event(
                "INFO",
                "aster.arrangement",
                "stage.complete",
                "Slot planning completed",
                stage="slot_planning",
                attempt=planning_attempt,
                slots=len(slots),
                elapsed_sec=elapsed,
            )
            try:
                attempt_stage = "dialogue_anchor_selection"
                stage_started = time.monotonic()
                slots = team.anchor_story(slots)
                context.set_artifact("edit_plan", slots)
                _write_json(edit_plan_path, slots)
                anchors = context.get_artifact("dialogue_anchors", [])
                _write_json(dialogue_anchors_path, anchors)
                anchor_seconds += time.monotonic() - stage_started

                attempt_stage = "retrieval"
                stage_started = time.monotonic()
                candidate_pool = team.scout(slots)
                context.set_artifact("edit_plan", slots)
                _write_json(edit_plan_path, slots)
                anchors = context.get_artifact("dialogue_anchors", [])
                _write_json(dialogue_anchors_path, anchors)
                retrieval_seconds += time.monotonic() - stage_started

                attempt_stage = "chronology_preflight"
                stage_started = time.monotonic()
                team.validate_composition(slots, candidate_pool)
                selection_seconds += time.monotonic() - stage_started

                attempt_stage = "beam_selection"
                stage_started = time.monotonic()
                beam_path, selection, pairwise_scores = team.compose(
                    slots,
                    candidate_pool,
                )
                selection_seconds += time.monotonic() - stage_started
                selection["planning_attempt"] = planning_attempt
                break
            except (NoFeasiblePathError, ValueError) as exc:
                elapsed = max(0.0, time.monotonic() - stage_started)
                if attempt_stage == "retrieval":
                    retrieval_seconds += elapsed
                else:
                    selection_seconds += elapsed
                if not isinstance(exc, NoFeasiblePathError) and attempt_stage != "retrieval":
                    raise
                failure = build_prompt_failure(
                    PromptFailureCode.PLANNING_ATTEMPT_INFEASIBLE,
                    attempt=planning_attempt,
                    stage=attempt_stage,
                    error_type=type(exc).__name__,
                    error_message=error_summary(exc),
                )
                diagnostics = (
                    exc.diagnostics
                    if isinstance(exc, NoFeasiblePathError)
                    else context.get_artifact("retrieval_failure", failure)
                )
                shortages = diagnostics.get("shortages") or {}
                failed_slot_ids = set(shortages)
                failed_slot_id = diagnostics.get("failed_slot_id")
                if failed_slot_id:
                    failed_slot_ids.add(str(failed_slot_id))
                failed_slots = [
                    {
                        "slot_id": slot["slot_id"],
                        "content_description": slot["content_description"],
                        "source_segment_ids": slot.get("source_segment_ids") or [],
                        "missing_candidates": shortages.get(slot["slot_id"]),
                    }
                    for slot in slots
                    if slot["slot_id"] in failed_slot_ids
                ]
                team.record_failure(
                    attempt=planning_attempt,
                    error=str(exc),
                    diagnostics=diagnostics,
                    failed_slots=failed_slots,
                )
                if planning_attempt > max_replans:
                    context.save_model_call_tree(status="failed")
                    raise
                log_event(
                    "WARNING",
                    "aster.composition",
                    "validation.reject",
                    "Planning attempt was infeasible; replanning with diagnostics",
                    failed_slots=sorted(failed_slot_ids),
                    **failure,
                )
        else:
            raise RuntimeError("Planning loop ended without a feasible path")

        _write_json(candidate_pool_path, candidate_pool)
        timings["slot_planning"] = planning_seconds
        timings["dialogue_anchor_selection"] = anchor_seconds
        timings["candidate_retrieval"] = retrieval_seconds

        stage_started = time.monotonic()
        raw_script = team.build_script(slots, beam_path)
        context.set_artifact("selection_diagnostics", selection)
        context.record_script_version(raw_script, source="beam_search")
        for _ in range(self.config.planners.script_review.review_rounds):
            raw_script, _ = team.revise(
                slots,
                candidate_pool,
                raw_script,
                pairwise_scores,
            )
        context.save_model_call_tree()
        context.save_model_usage()
        write_script(raw_script_path, raw_script)
        _write_json(selection_path, selection)
        timings["sequence_selection_and_review"] = (
            selection_seconds + time.monotonic() - stage_started
        )

        stage_started = time.monotonic()
        render_plan = compile_render_plan(
            request=request,
            raw_script=raw_script,
            music_profile=music_profile,
            video_description=video_description,
            config=self.config,
        )
        render_plan.write(render_plan_path)
        timings["plan_compilation"] = time.monotonic() - stage_started
        result = PlanningResult(
            status="success",
            render_plan=str(render_plan_path.resolve()),
            music_profile=str(music_profile_path.resolve()),
            edit_plan=str(edit_plan_path.resolve()),
            dialogue_anchors=str(dialogue_anchors_path.resolve()),
            candidate_pool=str(candidate_pool_path.resolve()),
            raw_script=str(raw_script_path.resolve()),
            selection_diagnostics=str(selection_path.resolve()),
            planning_history=str(planning_history_path.resolve()),
            planning_calls=str(planning_calls_path.resolve()),
            target_output_length_sec=request.target_output_length_sec,
            planned_output_length_sec=render_plan.duration_sec,
            num_raw_clips=len(raw_script),
            num_planned_clips=len(render_plan.clips),
            stage_timings_sec=timings,
            wall_clock_sec=time.monotonic() - started,
            music_memory=music_analysis.music_memory,
            model_usage=str(model_usage_path.resolve()),
            model_usage_summary=context.model_usage_summary(),
        )
        result.write(result_path)
        log_event(
            "SUCCESS",
            "planner",
            "stage.complete",
            "Planning completed with an immutable render plan",
            plan_id=render_plan.plan_id,
            clips=len(render_plan.clips),
            duration_sec=render_plan.duration_sec,
            elapsed_sec=result.wall_clock_sec,
        )
        return result


__all__ = ["Planner"]
