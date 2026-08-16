"""Public service coordinating the complete ASTER Planners stage."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import AppConfig
from cutmaster.infrastructure.observability.logging import error_summary, log_event
from cutmaster.workflow.contracts.planners import PlannersRequest, PlannersResult
from cutmaster.workflow.contracts.checkpoints import (
    PlannersCheckpoint,
    PlannersCheckpointStage,
    PlannersCheckpointStore,
)
from cutmaster.workflow.planners.aster_team import ASTERTeam
from cutmaster.workflow.planners.arrangement_architect import (
    prime_arrangement_context,
)
from cutmaster.workflow.planners.tools.errors import NoFeasiblePathError
from cutmaster.workflow.planners.tools.plan_compiler import (
    compile_render_plan,
    write_script,
)
from cutmaster.workflow.ports import (
    CancellationToken,
    ProgressReporter,
    ProgressUpdate,
    WorkflowCancelledError,
    raise_if_cancelled,
)
from cutmaster.workflow.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)
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


_CHECKPOINT_STAGE_RANK = {
    PlannersCheckpointStage.REPLAN_PENDING: 0,
    PlannersCheckpointStage.ARRANGEMENT: 1,
    PlannersCheckpointStage.STORY: 2,
    PlannersCheckpointStage.TIMELINE: 3,
    PlannersCheckpointStage.EDIT: 4,
    PlannersCheckpointStage.REVISION: 5,
}


def _checkpoint_feedback(value: Any) -> dict[str, Any] | None:
    """Remove the prompt-like retry instruction from persisted workflow state."""

    if not isinstance(value, dict):
        return None
    return {
        key: item
        for key, item in value.items()
        if key != "instruction"
    }


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
        cancellation_token: CancellationToken | None = None,
        checkpoint_store: PlannersCheckpointStore | None = None,
    ) -> PlannersResult:
        if checkpoint_store is not None and not isinstance(
            checkpoint_store,
            PlannersCheckpointStore,
        ):
            raise TypeError("checkpoint_store must implement PlannersCheckpointStore")
        raise_if_cancelled(cancellation_token)
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
        checkpoint = checkpoint_store.load() if checkpoint_store is not None else None
        timings = (
            {}
            if checkpoint is None
            else dict(checkpoint.stage_timings_sec)
        )
        context = WorkflowContext(
            planners_history_path,
            model_call_tree_path=planners_calls_path,
            model_usage_path=model_usage_path,
            stage_name="planners",
            prior_model_usage_summary=(
                None if checkpoint is None else checkpoint.prior_model_usage
            ),
            prior_model_call_count=(
                0 if checkpoint is None else checkpoint.prior_model_call_count
            ),
        )
        context.set_artifact("video_description", video_description)
        context.set_artifact("video_summary", video_summary)
        team = ASTERTeam(request.video_path, self.config, context)

        if checkpoint is None:
            stage_started = time.monotonic()
            raise_if_cancelled(cancellation_token)
            music_profile = team.profile_music(
                music_memory,
                request.target_output_length_sec,
                music_profile_path,
            )
            raise_if_cancelled(cancellation_token)
            timings["music_profile"] = time.monotonic() - stage_started
            slots: list[dict[str, Any]] = []
            dialogue_anchors: list[dict[str, Any]] = []
            candidate_pool: dict[str, list[dict[str, Any]]] = {}
            beam_path: list[dict[str, Any]] = []
            pairwise_scores: dict[str, dict[str, Any]] = {}
            selection: dict[str, Any] = {}
            raw_script: list[dict[str, Any]] = []
            completed_rank = 0
            first_aster_attempt = 1
        else:
            restored = checkpoint.to_dict()
            music_profile = dict(restored["music_profile"])
            slots = list(restored["slots"])
            dialogue_anchors = list(restored["dialogue_anchors"] or [])
            candidate_pool = dict(restored["candidate_pool"] or {})
            beam_path = list(restored["beam_path"] or [])
            pairwise_scores = dict(restored["pairwise_scores"] or {})
            selection = dict(restored["selection"] or {})
            raw_script = list(restored["raw_script"] or [])
            completed_rank = _CHECKPOINT_STAGE_RANK[checkpoint.completed_stage]
            first_aster_attempt = checkpoint.aster_attempt
            feedback = restored["planners_feedback"]
            if feedback is not None:
                context.set_artifact("planners_feedback", feedback)
            _write_json(music_profile_path, music_profile)
        if checkpoint is not None:
            prime_arrangement_context(request, music_profile, context)

        def save_boundary(
            stage: PlannersCheckpointStage,
            *,
            aster_attempt: int,
        ) -> None:
            if checkpoint_store is None:
                return
            rank = _CHECKPOINT_STAGE_RANK[stage]
            context.save_model_usage()
            checkpoint_store.save(
                PlannersCheckpoint(
                    completed_stage=stage,
                    aster_attempt=aster_attempt,
                    music_profile=music_profile,
                    slots=tuple(slots),
                    dialogue_anchors=(
                        tuple(dialogue_anchors) if rank >= 2 else None
                    ),
                    candidate_pool=(
                        {
                            slot_id: tuple(candidates)
                            for slot_id, candidates in candidate_pool.items()
                        }
                        if rank >= 3
                        else None
                    ),
                    beam_path=tuple(beam_path) if rank >= 4 else None,
                    selection=selection if rank >= 4 else None,
                    pairwise_scores=pairwise_scores if rank >= 4 else None,
                    raw_script=tuple(raw_script) if rank >= 5 else None,
                    planners_feedback=_checkpoint_feedback(
                        context.get_artifact("planners_feedback")
                    ),
                    stage_timings_sec=timings,
                    prior_model_usage=context.model_usage_summary(
                        include_prior=True
                    ),
                    prior_model_call_count=context.model_call_count(
                        include_prior=True
                    ),
                )
            )

        try:
            max_replans = self.config.planners.arrangement_architect.replan_max_rounds
            for aster_attempt in range(first_aster_attempt, max_replans + 2):
                if completed_rank < 1 or aster_attempt != first_aster_attempt:
                    raise_if_cancelled(cancellation_token)
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
                    timings["slot_arrangement"] = (
                        timings.get("slot_arrangement", 0.0) + elapsed
                    )
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
                    dialogue_anchors = []
                    candidate_pool = {}
                    beam_path = []
                    pairwise_scores = {}
                    selection = {}
                    raw_script = []
                    completed_rank = 1
                    save_boundary(
                        PlannersCheckpointStage.ARRANGEMENT,
                        aster_attempt=aster_attempt,
                    )
                    raise_if_cancelled(cancellation_token)
                else:
                    context.set_artifact("edit_plan", slots)
                    _write_json(edit_plan_path, slots)

                try:
                    retrieval_call_active = False
                    if completed_rank < 2:
                        _report_agent(
                            progress_reporter,
                            completed=1,
                            agent="story_editor",
                        )
                        attempt_stage = "dialogue_anchor_selection"
                        stage_started = time.monotonic()
                        raise_if_cancelled(cancellation_token)
                        slots = team.anchor_story(slots)
                        context.set_artifact("edit_plan", slots)
                        dialogue_anchors = list(
                            context.get_artifact("dialogue_anchors", [])
                        )
                        _write_json(edit_plan_path, slots)
                        _write_json(dialogue_anchors_path, dialogue_anchors)
                        timings["dialogue_anchor_selection"] = (
                            timings.get("dialogue_anchor_selection", 0.0)
                            + time.monotonic()
                            - stage_started
                        )
                        completed_rank = 2
                        save_boundary(
                            PlannersCheckpointStage.STORY,
                            aster_attempt=aster_attempt,
                        )
                        raise_if_cancelled(cancellation_token)
                    else:
                        context.set_artifact("dialogue_anchors", dialogue_anchors)
                        _write_json(dialogue_anchors_path, dialogue_anchors)

                    if completed_rank < 3:
                        _report_agent(
                            progress_reporter,
                            completed=2,
                            agent="timeline_scout",
                        )
                        attempt_stage = "retrieval"
                        stage_started = time.monotonic()
                        raise_if_cancelled(cancellation_token)
                        retrieval_call_active = True
                        candidate_pool = team.scout(slots)
                        retrieval_call_active = False
                        context.set_artifact("edit_plan", slots)
                        dialogue_anchors = list(
                            context.get_artifact("dialogue_anchors", [])
                        )
                        _write_json(edit_plan_path, slots)
                        _write_json(dialogue_anchors_path, dialogue_anchors)
                        timings["candidate_retrieval"] = (
                            timings.get("candidate_retrieval", 0.0)
                            + time.monotonic()
                            - stage_started
                        )
                        completed_rank = 3
                        save_boundary(
                            PlannersCheckpointStage.TIMELINE,
                            aster_attempt=aster_attempt,
                        )
                        raise_if_cancelled(cancellation_token)
                    else:
                        context.set_artifact("candidate_pool", candidate_pool)

                    if completed_rank < 4:
                        _report_agent(
                            progress_reporter,
                            completed=3,
                            agent="edit_composer",
                        )
                        attempt_stage = "chronology_preflight"
                        stage_started = time.monotonic()
                        raise_if_cancelled(cancellation_token)
                        team.validate_composition(slots, candidate_pool)
                        attempt_stage = "beam_selection"
                        beam_path, selection, pairwise_scores = team.compose(
                            slots,
                            candidate_pool,
                        )
                        selection["aster_attempt"] = aster_attempt
                        timings["sequence_selection"] = (
                            timings.get("sequence_selection", 0.0)
                            + time.monotonic()
                            - stage_started
                        )
                        completed_rank = 4
                        save_boundary(
                            PlannersCheckpointStage.EDIT,
                            aster_attempt=aster_attempt,
                        )
                        raise_if_cancelled(cancellation_token)
                    break
                except (NoFeasiblePathError, ValueError) as exc:
                    elapsed = max(0.0, time.monotonic() - stage_started)
                    timing_key = (
                        "candidate_retrieval"
                        if attempt_stage == "retrieval"
                        else "sequence_selection"
                    )
                    timings[timing_key] = timings.get(timing_key, 0.0) + elapsed
                    if (
                        not isinstance(exc, NoFeasiblePathError)
                        and not (
                            attempt_stage == "retrieval"
                            and retrieval_call_active
                        )
                    ):
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
                        raise
                    completed_rank = 0
                    save_boundary(
                        PlannersCheckpointStage.REPLAN_PENDING,
                        aster_attempt=aster_attempt + 1,
                    )
                    raise_if_cancelled(cancellation_token)
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
            if completed_rank < 5:
                stage_started = time.monotonic()
                raise_if_cancelled(cancellation_token)
                raw_script = team.build_script(slots, beam_path)
                context.set_artifact("selection_diagnostics", selection)
                context.record_script_version(raw_script, source="beam_search")
                _report_agent(
                    progress_reporter,
                    completed=4,
                    agent="revision_editor",
                )
                for _ in range(self.config.planners.script_review.review_rounds):
                    raise_if_cancelled(cancellation_token)
                    raw_script, _ = team.revise(
                        slots,
                        candidate_pool,
                        raw_script,
                        pairwise_scores,
                    )
                timings["revision_review"] = (
                    timings.get("revision_review", 0.0)
                    + time.monotonic()
                    - stage_started
                )
                completed_rank = 5
                context.save_model_call_tree()
                save_boundary(
                    PlannersCheckpointStage.REVISION,
                    aster_attempt=aster_attempt,
                )
                raise_if_cancelled(cancellation_token)

            context.save_model_call_tree()
            context.save_model_usage()
            write_script(raw_script_path, raw_script)
            _write_json(selection_path, selection)

            stage_started = time.monotonic()
            raise_if_cancelled(cancellation_token)
            render_plan = compile_render_plan(
                request=request,
                raw_script=raw_script,
                music_profile=music_profile,
                video_description=video_description,
                config=self.config,
            )
            raise_if_cancelled(cancellation_token)
            render_plan.write(render_plan_path)
            timings["plan_compilation"] = time.monotonic() - stage_started
            _report_agent(
                progress_reporter,
                completed=5,
                agent="revision_editor",
            )
        except BaseException as exc:
            try:
                context.save_model_usage()
                context.save_model_call_tree(
                    status=(
                        "interrupted"
                        if isinstance(exc, WorkflowCancelledError)
                        else "failed"
                    )
                )
            except Exception:
                pass
            raise

        result_timings = dict(timings)
        result_timings["sequence_selection_and_review"] = (
            result_timings.pop("sequence_selection", 0.0)
            + result_timings.pop("revision_review", 0.0)
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
            stage_timings_sec=result_timings,
            wall_clock_sec=time.monotonic() - started,
            music_memory_path=request.music.music_memory_path.resolve(),
            model_usage_path=model_usage_path.resolve(),
            model_usage_summary=context.model_usage_summary(),
            model_usage_cumulative_summary=context.model_usage_summary(
                include_prior=True
            ),
        )
        raise_if_cancelled(cancellation_token)
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
