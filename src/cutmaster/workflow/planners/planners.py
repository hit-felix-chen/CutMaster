"""Public service coordinating the complete ASTER Planners stage."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import AppConfig
from cutmaster.infrastructure.observability.logging import log_event
from cutmaster.workflow.contracts.planners import PlannersRequest, PlannersResult
from cutmaster.workflow.contracts.checkpoints import (
    PlannersCheckpoint,
    PlannersCheckpointStage,
    PlannersCheckpointStore,
    PlannersReplanScope,
)
from cutmaster.workflow.contracts.video import validate_video_description_document
from cutmaster.workflow.planners.aster_team import ASTERTeam
from cutmaster.workflow.planners.replan_policy import (
    ReplanState,
    decide_replan,
)
from cutmaster.workflow.planners.arrangement_architect import (
    prime_arrangement_context,
)
from cutmaster.workflow.planners.tools.errors import (
    GroupNoCandidateError,
    NoFeasiblePathError,
)
from cutmaster.workflow.planners.tools.plan_compiler import (
    compile_render_plan,
    write_script,
)
from cutmaster.workflow.planners.tools.planners_feedback import (
    build_stage_failure_diagnostics,
)
from cutmaster.workflow.ports import (
    CancellationToken,
    ProgressReporter,
    ProgressUpdate,
    WorkflowCancelledError,
    raise_if_cancelled,
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
    reporter.report(ProgressUpdate(completed, 4, agent, "agent"))


_CHECKPOINT_STAGE_RANK = {
    PlannersCheckpointStage.REPLAN_PENDING: 0,
    PlannersCheckpointStage.ARRANGEMENT: 1,
    PlannersCheckpointStage.STORY: 2,
    PlannersCheckpointStage.TIMELINE: 3,
    PlannersCheckpointStage.EDIT: 4,
    PlannersCheckpointStage.REVISION: 5,
}


def _checkpoint_feedback(value: Any) -> dict[str, Any] | None:
    """Keep failure evidence, without prompt instructions or obsolete hard bans."""

    if not isinstance(value, dict):
        return None
    return {
        key: item
        for key, item in value.items()
        if key not in {"instruction", "forbidden_group_segment_bindings"}
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
        arrangement_groups_path = output_dir / "arrangement_groups.json"
        planning_segments_path = output_dir / "planning_segments.json"
        planning_groups_path = output_dir / "planning_groups.json"
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
        validate_video_description_document(video_description)
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
        team = ASTERTeam(
            request.video_path,
            request.video.material.memory_root / "segments",
            self.config,
            context,
        )
        replan = ReplanState()

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
            arrangement_groups: list[dict[str, Any]] = []
            planning_segments: list[dict[str, Any]] = []
            planning_groups: list[dict[str, Any]] = []
            dialogue_anchors: list[dict[str, Any]] = []
            candidate_pool: dict[str, list[dict[str, Any]]] = {}
            beam_path: list[dict[str, Any]] = []
            pairwise_scores: dict[str, dict[str, Any]] = {}
            selection: dict[str, Any] = {}
            checkpoint_selected_trajectory_ids: dict[str, Any] = {}
            raw_script: list[dict[str, Any]] = []
            completed_rank = 0
            first_aster_attempt = 1
        else:
            restored = checkpoint.to_dict()
            replan.scope = checkpoint.replan_scope
            replan.local_attempt = checkpoint.local_replan_attempt
            replan.reuse = (
                None
                if restored["replan_reuse"] is None
                else dict(restored["replan_reuse"])
            )
            replan.pending = bool(
                checkpoint.completed_stage is PlannersCheckpointStage.REPLAN_PENDING
                and replan.scope is PlannersReplanScope.CANDIDATE_LOCAL
                and replan.reuse is not None
            )
            music_profile = dict(restored["music_profile"])
            slots = list(restored["slots"])
            arrangement_groups = list(restored["arrangement_groups"])
            planning_segments = list(restored["planning_segments"] or [])
            planning_groups = list(restored["planning_groups"] or [])
            dialogue_anchors = list(restored["dialogue_anchors"] or [])
            candidate_pool = dict(restored["candidate_pool"] or {})
            beam_path = list(restored["beam_path"] or [])
            pairwise_scores = dict(restored["pairwise_scores"] or {})
            selection = dict(restored["selection"] or {})
            checkpoint_selected_trajectory_ids = dict(
                restored["selected_trajectory_ids"] or {}
            )
            raw_script = list(restored["raw_script"] or [])
            completed_rank = _CHECKPOINT_STAGE_RANK[checkpoint.completed_stage]
            first_aster_attempt = checkpoint.aster_attempt
            feedback = restored["planners_feedback"]
            if feedback is not None:
                context.set_artifact("planners_feedback", feedback)
            _write_json(music_profile_path, music_profile)
            context.set_artifact("arrangement_groups", arrangement_groups)
            if completed_rank >= 2:
                context.set_artifact("planning_segments", planning_segments)
                context.set_artifact("planning_groups", planning_groups)
                context.set_artifact("dialogue_anchors", dialogue_anchors)
            if replan.pending:
                assert replan.reuse is not None
                slots = list(replan.reuse["previous_slots"])
                planning_segments = list(
                    replan.reuse["previous_planning_segments"]
                )
                planning_groups = list(
                    replan.reuse["previous_planning_groups"]
                )
                dialogue_anchors = list(
                    replan.reuse["previous_dialogue_anchors"]
                )
                candidate_pool = dict(
                    replan.reuse["previous_candidate_pool"]
                )
                context.set_artifact("planning_segments", planning_segments)
                context.set_artifact("planning_groups", planning_groups)
                context.set_artifact("dialogue_anchors", dialogue_anchors)
                context.set_artifact("candidate_pool", candidate_pool)
        if checkpoint is not None:
            prime_arrangement_context(request, music_profile, context)
            if completed_rank >= 4:
                restored_selection_ids = selection.get("selected_trajectory_ids")
                if not isinstance(restored_selection_ids, dict):
                    raise ValueError("Checkpoint selection has no trajectory mapping")
                if checkpoint_selected_trajectory_ids != restored_selection_ids:
                    raise ValueError(
                        "Checkpoint selected trajectory IDs disagree with selection"
                    )
            if checkpoint.completed_stage is PlannersCheckpointStage.REVISION:
                context.set_artifact("candidate_pool", candidate_pool)
                team.validate_revision_checkpoint(
                    slots,
                    candidate_pool,
                    beam_path,
                    selection,
                    raw_script,
                    pairwise_scores,
                )
            elif checkpoint.completed_stage is PlannersCheckpointStage.EDIT:
                context.set_artifact("candidate_pool", candidate_pool)
                team.validate_edit_checkpoint(
                    slots,
                    candidate_pool,
                    beam_path,
                    selection,
                    pairwise_scores,
                )
            elif completed_rank >= 2 or replan.pending:
                team.validate_planning(slots)

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
                    replan_scope=replan.scope,
                    local_replan_attempt=replan.local_attempt,
                    music_profile=music_profile,
                    slots=tuple(slots),
                    arrangement_groups=tuple(arrangement_groups),
                    planning_segments=(
                        tuple(planning_segments) if rank >= 2 else None
                    ),
                    planning_groups=(
                        tuple(planning_groups) if rank >= 2 else None
                    ),
                    dialogue_anchors=(
                        tuple(dialogue_anchors) if rank >= 2 else None
                    ),
                    candidate_pool=(
                        {
                            group_id: tuple(trajectories)
                            for group_id, trajectories in candidate_pool.items()
                        }
                        if rank >= 3
                        else None
                    ),
                    replan_reuse=(
                        replan.reuse
                        if stage
                        in {
                            PlannersCheckpointStage.REPLAN_PENDING,
                            PlannersCheckpointStage.STORY,
                        }
                        else None
                    ),
                    beam_path=tuple(beam_path) if rank >= 4 else None,
                    selected_trajectory_ids=(
                        dict(selection.get("selected_trajectory_ids") or {})
                        if rank >= 4
                        else None
                    ),
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
            max_aster_attempts = self.config.planners.aster_team.max_rounds
            max_local_replans = (
                self.config.planners.aster_team.max_local_replans
            )
            aster_attempt = first_aster_attempt

            def handle_attempt_failure(
                error: (
                    GroupNoCandidateError
                    | NoFeasiblePathError
                    | ValueError
                ),
                *,
                stage: str,
                attempt: int,
                stage_started: float,
                preserve_previous_groups: bool = False,
            ) -> None:
                nonlocal candidate_pool, beam_path, pairwise_scores
                nonlocal selection, raw_script, completed_rank
                nonlocal aster_attempt

                elapsed = max(0.0, time.monotonic() - stage_started)
                timing_key = {
                    "slot_arrangement": "slot_arrangement",
                    "dialogue_anchor_selection": "dialogue_anchor_selection",
                    "retrieval": "candidate_retrieval",
                }.get(stage, "sequence_selection")
                timings[timing_key] = timings.get(timing_key, 0.0) + elapsed

                raw_existing = getattr(error, "diagnostics", None)
                existing = raw_existing if isinstance(raw_existing, dict) else None
                previous_feedback = context.get_artifact("planners_feedback") or {}
                previous_diagnostics = previous_feedback.get("diagnostics") or {}
                diagnostics = build_stage_failure_diagnostics(
                    stage=stage,
                    error=error,
                    slots=slots,
                    existing=existing,
                    previous=(
                        previous_diagnostics
                        if isinstance(previous_diagnostics, dict)
                        else None
                    ),
                    preserve_previous_groups=preserve_previous_groups,
                )
                failed_group_ids = set(diagnostics["failed_group_ids"])
                failed_parent_group_ids = set(
                    diagnostics["failed_parent_group_ids"]
                )
                previous_local_replan_attempt = replan.local_attempt
                decision = decide_replan(
                    aster_attempt=attempt,
                    local_attempt=replan.local_attempt,
                    max_local_attempts=max_local_replans,
                    candidate_failure=isinstance(error, GroupNoCandidateError),
                    has_failed_groups=bool(failed_parent_group_ids),
                )
                diagnostics["aster_attempt"] = attempt
                diagnostics["replan_scope"] = decision.scope.value
                diagnostics["local_replan_attempt"] = decision.local_attempt
                valid_counts = diagnostics.get("valid_trajectory_counts") or {}
                failed_slots = [
                    {
                        **slot,
                        "parent_group_id": (
                            slot.get("parent_group_id") or slot.get("group_id")
                        ),
                        "valid_trajectory_count": valid_counts.get(
                            str(slot.get("group_id") or "")
                        ),
                    }
                    for slot in slots
                    if (
                        str(slot.get("parent_group_id") or slot.get("group_id") or "")
                        in failed_parent_group_ids
                    )
                ]
                team.record_failure(
                    attempt=attempt,
                    error=str(error),
                    diagnostics=diagnostics,
                    failed_slots=failed_slots,
                )
                if not decision.is_local and attempt >= max_aster_attempts:
                    raise error

                if decision.is_local:
                    partial_pool = context.get_artifact("candidate_pool")
                    if isinstance(partial_pool, dict):
                        candidate_pool = {
                            str(group_id): list(trajectories)
                            for group_id, trajectories in partial_pool.items()
                            if isinstance(trajectories, list)
                        }
                    replan.reuse = {
                        "previous_slots": list(slots),
                        "previous_planning_segments": list(planning_segments),
                        "previous_planning_groups": list(planning_groups),
                        "previous_dialogue_anchors": list(dialogue_anchors),
                        "previous_candidate_pool": dict(candidate_pool),
                        "affected_parent_group_ids": [],
                    }
                else:
                    # A global retry is a new complete ASTER flow.  No prior
                    # group repair or candidate pool may leak into it.
                    candidate_pool = {}
                replan.apply(decision)
                beam_path = []
                pairwise_scores = {}
                selection = {}
                raw_script = []
                context.set_artifact("candidate_rejections", [])
                context.set_artifact("retrieval_summary", {})
                context.set_artifact("retrieval_failure", None)
                context.set_artifact("candidate_pool", candidate_pool)
                context.set_artifact("pairwise_scores", {})
                context.set_artifact("selected_trajectory_ids", {})
                context.set_artifact("selection_diagnostics", {})
                completed_rank = 0
                aster_attempt = decision.aster_attempt
                if slots and arrangement_groups:
                    save_boundary(
                        PlannersCheckpointStage.REPLAN_PENDING,
                        aster_attempt=decision.aster_attempt,
                    )
                raise_if_cancelled(cancellation_token)
                log_event(
                    "WARNING",
                    "aster.composition",
                    "fallback.apply",
                    (
                        "Candidate-empty local repair scheduled"
                        if decision.is_local
                        else "ASTER attempt failed; returning to Arrangement with diagnostics"
                    ),
                    attempt=attempt,
                    stage=stage,
                    next_attempt=decision.aster_attempt,
                    replan_scope=decision.scope.value,
                    previous_local_replan_attempt=previous_local_replan_attempt,
                    local_replan_attempt=decision.local_attempt,
                    targeted_repair=replan.pending,
                    failed_group_ids=sorted(failed_group_ids),
                    failed_parent_group_ids=sorted(failed_parent_group_ids),
                    reason_code=diagnostics["reason_code"],
                    diagnosis=diagnostics["diagnosis"],
                    repair_requirement=diagnostics["repair_requirement"],
                )

            while aster_attempt <= max_aster_attempts:
                if replan.pending:
                    feedback = context.get_artifact("planners_feedback") or {}
                    diagnostics = feedback.get("diagnostics") or {}
                    previous_slots = list(slots)
                    previous_arrangement_groups = list(arrangement_groups)
                    previous_reuse = replan.reuse
                    assert previous_reuse is not None
                    context.set_artifact(
                        "planning_segments",
                        list(previous_reuse["previous_planning_segments"]),
                    )
                    context.set_artifact(
                        "planning_groups",
                        list(previous_reuse["previous_planning_groups"]),
                    )
                    context.set_artifact(
                        "dialogue_anchors",
                        list(previous_reuse["previous_dialogue_anchors"]),
                    )
                    context.set_artifact(
                        "candidate_pool",
                        dict(previous_reuse["previous_candidate_pool"]),
                    )
                    raise_if_cancelled(cancellation_token)
                    _report_agent(
                        progress_reporter,
                        completed=0,
                        agent="arrangement_architect",
                    )
                    stage_started = time.monotonic()
                    try:
                        redesigned_slots, replanned_slot_ids = team.repair_groups(
                            slots,
                            diagnostics,
                        )
                    except ValueError as exc:
                        handle_attempt_failure(
                            exc,
                            stage="slot_arrangement",
                            attempt=aster_attempt,
                            stage_started=stage_started,
                            preserve_previous_groups=True,
                        )
                        continue
                    arrangement_groups = list(
                        context.get_artifact("arrangement_groups") or []
                    )
                    timings["group_replanning"] = (
                        timings.get("group_replanning", 0.0)
                        + time.monotonic()
                        - stage_started
                    )
                    beam_path = []
                    pairwise_scores = {}
                    selection = {}
                    raw_script = []
                    _report_agent(
                        progress_reporter,
                        completed=1,
                        agent="story_editor",
                    )
                    story_started = time.monotonic()
                    try:
                        (
                            slots,
                            affected_parent_group_ids,
                        ) = team.refresh_story_groups(
                            redesigned_slots,
                            previous_slots=previous_slots,
                            replanned_slot_ids=replanned_slot_ids,
                        )
                    except Exception as exc:
                        arrangement_groups = previous_arrangement_groups
                        context.set_artifact(
                            "arrangement_groups",
                            arrangement_groups,
                        )
                        if not isinstance(exc, ValueError):
                            raise
                        handle_attempt_failure(
                            exc,
                            stage="dialogue_anchor_selection",
                            attempt=aster_attempt,
                            stage_started=story_started,
                            preserve_previous_groups=True,
                        )
                        continue
                    planning_segments = list(
                        context.get_artifact("planning_segments") or []
                    )
                    planning_groups = list(
                        context.get_artifact("planning_groups") or []
                    )
                    dialogue_anchors = list(
                        context.get_artifact("dialogue_anchors", [])
                    )
                    replan.reuse = {
                        **previous_reuse,
                        "affected_parent_group_ids": sorted(
                            affected_parent_group_ids
                        ),
                    }
                    replan.pending = False
                    completed_rank = 2
                    timings["dialogue_anchor_selection"] = (
                        timings.get("dialogue_anchor_selection", 0.0)
                        + time.monotonic()
                        - story_started
                    )
                    _write_json(edit_plan_path, slots)
                    _write_json(
                        arrangement_groups_path,
                        arrangement_groups,
                    )
                    _write_json(planning_segments_path, planning_segments)
                    _write_json(planning_groups_path, planning_groups)
                    _write_json(dialogue_anchors_path, dialogue_anchors)
                    save_boundary(
                        PlannersCheckpointStage.STORY,
                        aster_attempt=aster_attempt,
                    )
                    raise_if_cancelled(cancellation_token)
                elif completed_rank < 1:
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
                    arrangement_groups = list(
                        context.get_artifact("arrangement_groups") or []
                    )
                    _write_json(edit_plan_path, slots)
                    _write_json(arrangement_groups_path, arrangement_groups)
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
                    planning_segments = []
                    planning_groups = []
                    candidate_pool = {}
                    # A full Arrangement pass replaces the planning contract.
                    # Reuse is only safe after a targeted group repair.
                    replan.clear_scope()
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
                    context.set_artifact("arrangement_groups", arrangement_groups)
                    _write_json(edit_plan_path, slots)
                    _write_json(arrangement_groups_path, arrangement_groups)

                try:
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
                        planning_segments = list(
                            context.get_artifact("planning_segments") or []
                        )
                        planning_groups = list(
                            context.get_artifact("planning_groups") or []
                        )
                        dialogue_anchors = list(
                            context.get_artifact("dialogue_anchors", [])
                        )
                        _write_json(edit_plan_path, slots)
                        _write_json(planning_segments_path, planning_segments)
                        _write_json(planning_groups_path, planning_groups)
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
                        context.set_artifact("planning_segments", planning_segments)
                        context.set_artifact("planning_groups", planning_groups)
                        context.set_artifact("dialogue_anchors", dialogue_anchors)
                        _write_json(planning_segments_path, planning_segments)
                        _write_json(planning_groups_path, planning_groups)
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
                        if replan.reuse is None:
                            candidate_pool = team.scout(
                                slots,
                                cancellation_token,
                            )
                        else:
                            candidate_pool = team.scout_with_reuse(
                                slots,
                                previous_candidate_pool=dict(
                                    replan.reuse[
                                        "previous_candidate_pool"
                                    ]
                                ),
                                previous_slots=list(
                                    replan.reuse["previous_slots"]
                                ),
                                previous_planning_groups=list(
                                    replan.reuse[
                                        "previous_planning_groups"
                                    ]
                                ),
                                previous_planning_segments=list(
                                    replan.reuse[
                                        "previous_planning_segments"
                                    ]
                                ),
                                affected_parent_group_ids=set(
                                    replan.reuse[
                                        "affected_parent_group_ids"
                                    ]
                                ),
                                cancellation_token=cancellation_token,
                            )
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
                        replan.clear_scope()
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
                except (
                    GroupNoCandidateError,
                    NoFeasiblePathError,
                ) as exc:
                    handle_attempt_failure(
                        exc,
                        stage=attempt_stage,
                        attempt=aster_attempt,
                        stage_started=stage_started,
                    )
            _write_json(candidate_pool_path, candidate_pool)
            if completed_rank < 5:
                raise_if_cancelled(cancellation_token)
                raw_script = team.build_script(slots, beam_path)
                context.set_artifact("selection_diagnostics", selection)
                context.record_script_version(raw_script, source="composition")

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
                completed=4,
                agent="edit_composer",
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
        result = PlannersResult(
            status="success",
            render_plan=render_plan,
            render_plan_path=render_plan_path.resolve(),
            music_profile_path=music_profile_path.resolve(),
            edit_plan_path=edit_plan_path.resolve(),
            planning_segments_path=planning_segments_path.resolve(),
            planning_groups_path=planning_groups_path.resolve(),
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
