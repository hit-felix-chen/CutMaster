from __future__ import annotations

from pathlib import Path
from typing import Any

from cutmaster.planner.candidate_retrieval import retrieve_candidates
from cutmaster.planner.dialogue_anchors import select_dialogue_anchors
from cutmaster.planner.media import SegmentMediaReader
from cutmaster.configuration.schema import AppConfig
from cutmaster.contracts.workflow import RunRequest
from cutmaster.runtime.observability import log_event
from cutmaster.runtime.workflow_context import WorkflowContext
from cutmaster.planner.script_review import review_and_patch
from cutmaster.planner.sequence_selection import (
    NoFeasiblePathError,
    path_to_script,
    select_paths,
    validate_chronological_path,
)
from cutmaster.planner.slot_planning import (
    align_slots_to_music,
    plan_edit_slots,
    redesign_edit_slots,
)


def _merge_planning_feedback(
    previous: dict[str, Any] | None,
    *,
    attempt: int,
    error: str,
    diagnostics: dict[str, Any],
    failed_slots: list[dict[str, Any]],
    candidates_per_slot: int,
) -> dict[str, Any]:
    previous = previous or {}
    accumulated: dict[tuple[str, ...], dict[str, Any]] = {}
    unassigned: list[dict[str, Any]] = []
    for failed in [*(previous.get("failed_slots") or []), *failed_slots]:
        segment_ids = tuple(
            str(value) for value in failed.get("source_segment_ids") or []
        )
        if segment_ids:
            accumulated[segment_ids] = failed
        else:
            unassigned.append(failed)

    forbidden_segment_ids = {
        str(value) for value in previous.get("forbidden_segment_ids") or []
    }
    forbidden_segment_ids.update(
        segment_id
        for failed in failed_slots
        if failed.get("missing_candidates") == candidates_per_slot
        for segment_id in failed.get("source_segment_ids") or []
    )
    failure_history = list(previous.get("failure_history") or [])
    failure_history.append(
        {
            "attempt": attempt,
            "error": error,
            "diagnostics": diagnostics,
            "failed_slots": failed_slots,
        }
    )
    return {
        "attempt": attempt,
        "error": error,
        "diagnostics": diagnostics,
        "current_failed_slots": failed_slots,
        "failed_slots": [*accumulated.values(), *unassigned],
        "forbidden_segment_ids": sorted(forbidden_segment_ids),
        "failure_history": failure_history,
        "instruction": (
            "Replan with supported source Segments in source order. Avoid every Segment "
            "assignment accumulated across earlier candidate shortages or empty "
            "chronological paths."
        ),
    }


class Planner:
    """Single planning facade over the four independent planning stages."""

    def __init__(
        self,
        video_path: Path,
        config: AppConfig,
        context: WorkflowContext,
    ) -> None:
        self.video_path = video_path
        self.config = config
        self.context = context
        video_description = context.get_artifact("video_description")
        if video_description is None:
            raise RuntimeError(
                "Video description must be available before Planner initialization"
            )
        self.media = SegmentMediaReader(video_path, video_description)

    def plan_slots(
        self,
        request: RunRequest,
        music_profile: dict[str, Any],
    ) -> list[dict[str, Any]]:
        slots = plan_edit_slots(
            request,
            music_profile,
            self.config.llm,
            self.context,
            target_clip_duration_sec=(
                self.config.slot_planning.target_clip_duration_sec
            ),
        )
        return align_slots_to_music(
            slots,
            music_profile,
            request.target_output_length_sec,
            self.config.render.fps,
            self.config.slot_planning.target_clip_duration_sec,
        )

    def retrieve(self, slots: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        return retrieve_candidates(
            slots,
            self.media,
            self.config.llm,
            self.config.vlm,
            self.config.candidate_retrieval,
            self.context,
            replan_slots=self._redesign_slots_and_refresh_anchors,
        )

    def _redesign_slots_and_refresh_anchors(
        self,
        slots: list[dict[str, Any]],
        failures: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], set[str]]:
        redesigned_slots, replanned_slot_ids = redesign_edit_slots(
            slots,
            failures,
            self.config.llm,
            self.context,
        )
        previous_by_id = {
            str(slot["slot_id"]): slot
            for slot in slots
        }
        redesigned_by_id = {
            str(slot["slot_id"]): slot
            for slot in redesigned_slots
        }
        moved_anchor_slot_ids = {
            slot_id
            for slot_id in replanned_slot_ids
            if previous_by_id[slot_id].get("dialogue_anchor") is not None
            if str(
                previous_by_id[slot_id]["dialogue_anchor"][
                    "source_segment_id"
                ]
            )
            not in {
                str(segment_id)
                for segment_id in redesigned_by_id[slot_id][
                    "source_segment_ids"
                ]
            }
        }
        if not moved_anchor_slot_ids:
            return redesigned_slots, replanned_slot_ids

        previous_fixed_candidates = {
            slot_id: slot.get("fixed_candidate")
            for slot_id, slot in previous_by_id.items()
        }
        refreshed_slots = self.anchor_dialogue(redesigned_slots)
        refreshed_by_id = {
            str(slot["slot_id"]): slot
            for slot in refreshed_slots
        }
        changed_anchor_slot_ids = {
            slot_id
            for slot_id in previous_by_id
            if previous_fixed_candidates[slot_id]
            != refreshed_by_id[slot_id].get("fixed_candidate")
        }
        reset_slot_ids = {
            *replanned_slot_ids,
            *changed_anchor_slot_ids,
        }
        log_event(
            "WARNING",
            "planner.anchor",
            "fallback.apply",
            "Re-ran dialogue-anchor selection after anchored Segments moved",
            moved_anchor_slot_ids=sorted(moved_anchor_slot_ids),
            changed_anchor_slot_ids=sorted(changed_anchor_slot_ids),
            reset_slot_ids=sorted(reset_slot_ids),
        )
        return refreshed_slots, reset_slot_ids

    def anchor_dialogue(
        self,
        slots: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return select_dialogue_anchors(
            slots,
            self.config.llm,
            self.config.dialogue_anchors,
            self.context,
        )

    def validate_sequence(
        self,
        slots: list[dict[str, Any]],
        candidate_pool: dict[str, list[dict[str, Any]]],
    ) -> None:
        validate_chronological_path(slots, candidate_pool)

    def select(
        self,
        slots: list[dict[str, Any]],
        candidate_pool: dict[str, list[dict[str, Any]]],
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, Any],
        dict[str, dict[str, Any]],
    ]:
        return select_paths(
            self.media,
            slots,
            candidate_pool,
            self.config.beam_search.beam_width,
            self.config.vlm,
            self.context,
            sample_frames=self.config.candidate_retrieval.visual_sample_frames,
        )

    def build_script(
        self,
        slots: list[dict[str, Any]],
        selected_path: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return path_to_script(slots, selected_path, self.video_path)

    def review(
        self,
        slots: list[dict[str, Any]],
        candidate_pool: dict[str, list[dict[str, Any]]],
        script: list[dict[str, Any]],
        pairwise_scores: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return review_and_patch(
            slots,
            candidate_pool,
            script,
            self.config.llm,
            self.context,
            pairwise_scores,
        )

    def record_failure(
        self,
        *,
        attempt: int,
        error: str,
        diagnostics: dict[str, Any],
        failed_slots: list[dict[str, Any]],
    ) -> None:
        feedback = _merge_planning_feedback(
            self.context.get_artifact("planning_feedback"),
            attempt=attempt,
            error=error,
            diagnostics=diagnostics,
            failed_slots=failed_slots,
            candidates_per_slot=self.config.candidate_retrieval.candidates_per_slot,
        )
        self.context.set_artifact("planning_feedback", feedback)


__all__ = [
    "NoFeasiblePathError",
    "Planner",
    "align_slots_to_music",
    "path_to_script",
    "plan_edit_slots",
    "redesign_edit_slots",
    "retrieve_candidates",
    "review_and_patch",
    "select_paths",
    "select_dialogue_anchors",
    "validate_chronological_path",
]
