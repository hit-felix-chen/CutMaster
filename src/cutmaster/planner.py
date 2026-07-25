from __future__ import annotations

from pathlib import Path
from typing import Any

from cutmaster.candidate_retriever import retrieve_candidates
from cutmaster.models import AppConfig, RunRequest
from cutmaster.workflow_context import WorkflowContext
from cutmaster.script_reviewer import review_and_patch
from cutmaster.sequence_selector import (
    NoFeasiblePathError,
    path_to_script,
    precompute_pairwise_scores,
    select_paths,
    validate_chronological_path,
)
from cutmaster.slot_planner import align_slots_to_music, plan_edit_slots


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
        )
        return align_slots_to_music(
            slots,
            music_profile,
            request.target_output_length_sec,
            self.config.render.fps,
        )

    def retrieve(self, slots: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        return retrieve_candidates(
            slots,
            self.video_path,
            self.config.llm,
            self.config.vlm,
            self.config.candidate_retrieval,
            self.context,
        )

    def validate_sequence(
        self,
        slots: list[dict[str, Any]],
        candidate_pool: dict[str, list[dict[str, Any]]],
    ) -> None:
        validate_chronological_path(slots, candidate_pool)

    def score_pairs(
        self,
        slots: list[dict[str, Any]],
        candidate_pool: dict[str, list[dict[str, Any]]],
    ) -> dict[str, dict[str, Any]]:
        return precompute_pairwise_scores(
            self.video_path,
            slots,
            candidate_pool,
            self.config.vlm,
            self.context,
            sample_frames=self.config.candidate_retrieval.visual_sample_frames,
        )

    def select(
        self,
        slots: list[dict[str, Any]],
        candidate_pool: dict[str, list[dict[str, Any]]],
        pairwise_scores: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        return select_paths(
            slots,
            candidate_pool,
            self.config.beam_search.beam_width,
            pairwise_scores,
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
    "precompute_pairwise_scores",
    "retrieve_candidates",
    "review_and_patch",
    "select_paths",
    "validate_chronological_path",
]
