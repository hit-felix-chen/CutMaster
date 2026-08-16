"""Public service coordinating the complete ASTER Planners stage."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import AppConfig
from cutmaster.workflow.contracts.planners import PlannersRequest, PlannersResult
from cutmaster.workflow.planners.aster_team import ASTERTeam
from cutmaster.workflow.planners.tools.plan_compiler import (
    compile_render_plan,
    write_script,
)
from cutmaster.workflow.planners.tools.errors import NoFeasiblePathError
from cutmaster.workflow.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)
from cutmaster.infrastructure.observability.logging import error_summary, log_event
from cutmaster.workflow.ports import ProgressReporter, ProgressUpdate
from cutmaster.workflow.shared.execution_context import WorkflowContext


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    resolved = path.resolve()
    if not resolved.is_file() or resolved.is_symlink():
        raise FileNotFoundError(f"{label} is unavailable: {resolved}")
    value = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{label} must contain one JSON object")
    return value


def _validate_request(request: PlannersRequest) -> None:
    for source, label in (
        (request.video_path, "Video Material source"),
        (request.audio_path, "Music Material source"),
    ):
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError(f"{label} is unavailable: {source}")
    for memory, label in (
        (request.video.video_description_path, "Video Description"),
        (request.video.video_summary_path, "Video Summary"),
        (request.video.dialogues_path, "Dialogue Memory"),
        (request.music.music_memory_path, "Music Memory"),
    ):
        if not memory.is_file() or memory.is_symlink():
            raise FileNotFoundError(f"{label} is unavailable: {memory}")
    output_dir = request.workspace.root.resolve()
    if output_dir.exists() and (not output_dir.is_dir() or output_dir.is_symlink()):
        raise ValueError(f"Planners workspace is not a regular directory: {output_dir}")


def _report_agent(
    reporter: ProgressReporter | None,
    *,
    completed: int,
    agent: str,
) -> None:
    if reporter is None:
        return
    reporter.report(ProgressUpdate(completed, 5, agent, "agent"))


class Planners:
    """Make all semantic and frame-timing edit decisions."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def plan(
        self,
        request: PlannersRequest,
        *,
        overwrite: bool = False,
        progress_reporter: ProgressReporter | None = None,
    ) -> PlannersResult:
        _validate_request(request)
        output_dir = request.output_dir.resolve()
        diagnostics_dir = output_dir / "diagnostics"
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        render_plan_path = output_dir / "render_plan.json"
        if render_plan_path.exists() and not overwrite:
            raise FileExistsError(
                "Planners workspace already contains a RenderPlan; request a new "
                f"workspace or enable overwrite: {render_plan_path}"
            )
        planners_history_path = diagnostics_dir / "planners_history.json"
        planners_calls_path = diagnostics_dir / "planners_calls.json"
        model_usage_path = diagnostics_dir / "model_usage.json"
        if overwrite:
            for stale_path in (
                planners_history_path,
                planners_calls_path,
                model_usage_path,
            ):
                stale_path.unlink(missing_ok=True)
        music_profile_path = output_dir / "music_profile.json"
        edit_plan_path = output_dir / "edit_plan.json"
        dialogue_anchors_path = output_dir / "dialogue_anchors.json"
        candidate_pool_path = output_dir / "candidate_pool.json"
        raw_script_path = output_dir / "script_raw.json"
        selection_path = diagnostics_dir / "selection_diagnostics.json"
        result_path = output_dir / "planners_result.json"

        started = time.monotonic()
        timings: dict[str, float] = {}
        video_description = _read_json_object(
            request.video.video_description_path,
            "Video Description",
        )
        video_summary = _read_json_object(
            request.video.video_summary_path,
            "Video Summary",
        )
        music_memory = _read_json_object(
            request.music.music_memory_path,
            "Music Memory",
        )
        context = WorkflowContext(
            planners_history_path,
            model_call_tree_path=planners_calls_path,
            model_usage_path=model_usage_path,
            stage_name="planners",
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

        arrangement_seconds = 0.0
        anchor_seconds = 0.0
        retrieval_seconds = 0.0
        selection_seconds = 0.0
        slots: list[dict[str, Any]] = []
        candidate_pool: dict[str, list[dict[str, Any]]] = {}
        beam_path: list[dict[str, Any]] = []
        pairwise_scores: dict[str, dict[str, Any]] = {}
        selection: dict[str, Any] = {}
        max_replans = self.config.planners.arrangement_architect.replan_max_rounds
        for aster_attempt in range(1, max_replans + 2):
            _report_agent(
                progress_reporter,
                completed=0,
                agent="arrangement_architect",
            )
            stage_started = time.monotonic()
            log_event(
                "INFO",
                "aster.arrangement",
                "stage.start",
                "Slot arrangement started",
                stage="slot_arrangement",
                attempt=aster_attempt,
            )
            slots = team.arrange(request, music_profile)
            context.set_artifact("edit_plan", slots)
            _write_json(edit_plan_path, slots)
            elapsed = time.monotonic() - stage_started
            arrangement_seconds += elapsed
            log_event(
                "INFO",
                "aster.arrangement",
                "stage.complete",
                "Slot arrangement completed",
                stage="slot_arrangement",
                attempt=aster_attempt,
                slots=len(slots),
                elapsed_sec=elapsed,
            )
            try:
                _report_agent(
                    progress_reporter,
                    completed=1,
                    agent="story_editor",
                )
                attempt_stage = "dialogue_anchor_selection"
                stage_started = time.monotonic()
                slots = team.anchor_story(slots)
                context.set_artifact("edit_plan", slots)
                _write_json(edit_plan_path, slots)
                anchors = context.get_artifact("dialogue_anchors", [])
                _write_json(dialogue_anchors_path, anchors)
                anchor_seconds += time.monotonic() - stage_started

                _report_agent(
                    progress_reporter,
                    completed=2,
                    agent="timeline_scout",
                )
                attempt_stage = "retrieval"
                stage_started = time.monotonic()
                candidate_pool = team.scout(slots)
                context.set_artifact("edit_plan", slots)
                _write_json(edit_plan_path, slots)
                anchors = context.get_artifact("dialogue_anchors", [])
                _write_json(dialogue_anchors_path, anchors)
                retrieval_seconds += time.monotonic() - stage_started

                _report_agent(
                    progress_reporter,
                    completed=3,
                    agent="edit_composer",
                )
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
                selection["aster_attempt"] = aster_attempt
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
                    PromptFailureCode.PLANNERS_STAGE_ATTEMPT_INFEASIBLE,
                    attempt=aster_attempt,
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
                    attempt=aster_attempt,
                    error=str(exc),
                    diagnostics=diagnostics,
                    failed_slots=failed_slots,
                )
                if aster_attempt > max_replans:
                    context.save_model_call_tree(status="failed")
                    raise
                log_event(
                    "WARNING",
                    "aster.composition",
                    "validation.reject",
                    "ASTER attempt was infeasible; retrying with diagnostics",
                    failed_slots=sorted(failed_slot_ids),
                    **failure,
                )
        else:
            raise RuntimeError("ASTER loop ended without a feasible path")

        _write_json(candidate_pool_path, candidate_pool)
        timings["slot_arrangement"] = arrangement_seconds
        timings["dialogue_anchor_selection"] = anchor_seconds
        timings["candidate_retrieval"] = retrieval_seconds

        stage_started = time.monotonic()
        raw_script = team.build_script(slots, beam_path)
        context.set_artifact("selection_diagnostics", selection)
        context.record_script_version(raw_script, source="beam_search")
        _report_agent(
            progress_reporter,
            completed=4,
            agent="revision_editor",
        )
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
        _report_agent(
            progress_reporter,
            completed=5,
            agent="revision_editor",
        )
        result = PlannersResult(
            status="success",
            render_plan=render_plan,
            render_plan_path=render_plan_path.resolve(),
            music_profile_path=music_profile_path.resolve(),
            edit_plan_path=edit_plan_path.resolve(),
            dialogue_anchors_path=dialogue_anchors_path.resolve(),
            candidate_pool_path=candidate_pool_path.resolve(),
            raw_script_path=raw_script_path.resolve(),
            selection_diagnostics_path=selection_path.resolve(),
            planners_history_path=planners_history_path.resolve(),
            planners_calls_path=planners_calls_path.resolve(),
            target_output_length_sec=request.target_output_length_sec,
            planned_output_length_sec=render_plan.duration_sec,
            num_raw_clips=len(raw_script),
            num_planned_clips=len(render_plan.clips),
            stage_timings_sec=timings,
            wall_clock_sec=time.monotonic() - started,
            music_memory_path=request.music.music_memory_path.resolve(),
            model_usage_path=model_usage_path.resolve(),
            model_usage_summary=context.model_usage_summary(),
            model_usage_cumulative_summary=context.model_usage_summary(
                include_prior=True
            ),
        )
        result.write(result_path)
        log_event(
            "SUCCESS",
            "planners",
            "stage.complete",
            "Planners completed an immutable render plan",
            plan_id=render_plan.plan_id,
            clips=len(render_plan.clips),
            duration_sec=render_plan.duration_sec,
            elapsed_sec=result.wall_clock_sec,
        )
        return result


__all__ = ["Planners"]
