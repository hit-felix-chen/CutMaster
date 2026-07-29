from __future__ import annotations

from pathlib import Path
from typing import Any

from cutmaster.planners.timeline_scout import TimelineScoutAgent
from cutmaster.planners.story_editor import StoryEditorAgent
from cutmaster.planners.tools.segment_media import SegmentMediaReader
from cutmaster.configuration.schema import AppConfig
from cutmaster.contracts.workflow import RunRequest
from cutmaster.runtime.observability import log_event
from cutmaster.runtime.workflow_context import WorkflowContext
from cutmaster.planners.revision_editor import RevisionEditorAgent
from cutmaster.planners.edit_composer import EditComposerAgent
from cutmaster.planners.tools.errors import NoFeasiblePathError
from cutmaster.planners.tools.planning_feedback import merge_planning_feedback
from cutmaster.planners.arrangement_architect import (
    ArrangementArchitectAgent,
)


class ASTERTeam:
    """Coordinate the five specialized ASTER planning agents."""

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
                "Video description must be available before ASTERTeam initialization"
            )
        self.media = SegmentMediaReader(video_path, video_description)
        self.arrangement_architect = ArrangementArchitectAgent(config, context)
        self.story_editor = StoryEditorAgent(config, context)
        self.edit_composer = EditComposerAgent(
            self.media,
            video_path,
            config,
            context,
        )
        self.revision_editor = RevisionEditorAgent(config, context)
        self.timeline_scout = TimelineScoutAgent(
            self.media,
            config,
            context,
            repair_slots=self._redesign_slots_and_refresh_anchors,
        )

    def profile_music(
        self,
        audio_path: Path,
        target_duration_sec: float,
        output_path: Path,
    ) -> dict[str, Any]:
        return self.arrangement_architect.profile_music(
            audio_path,
            target_duration_sec,
            output_path,
        )

    def _warn_about_missing_shot_annotations(self) -> None:
        video_description = self.context.get_artifact("video_description") or {}
        missing = [
            (str(segment["segment_id"]), str(shot["shot_id"]))
            for segment in video_description.get("segments", [])
            for shot in segment.get("shots", [])
            if shot.get("visual_annotation_status", "complete") != "complete"
        ]
        if not missing:
            return
        preview_limit = 20
        log_event(
            "WARNING",
            "aster.arrangement",
            "fallback.apply",
            "Planning is continuing with Shots that lack visual annotations",
            missing_shots=len(missing),
            total_shots=sum(
                len(segment.get("shots", []))
                for segment in video_description.get("segments", [])
            ),
            affected_segments=len({segment_id for segment_id, _ in missing}),
            missing_shot_ids_preview=[
                shot_id for _, shot_id in missing[:preview_limit]
            ],
            omitted_shot_ids=max(0, len(missing) - preview_limit),
            reason="data_inspection_failed",
        )

    def arrange(
        self,
        request: RunRequest,
        music_profile: dict[str, Any],
    ) -> list[dict[str, Any]]:
        self._warn_about_missing_shot_annotations()
        return self.arrangement_architect.arrange(request, music_profile)

    def scout(self, slots: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        return self.timeline_scout.scout(slots)

    def _redesign_slots_and_refresh_anchors(
        self,
        slots: list[dict[str, Any]],
        failures: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], set[str]]:
        redesigned_slots, replanned_slot_ids = self.arrangement_architect.repair(
            slots,
            failures,
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
        refreshed_slots = self.story_editor.anchor(redesigned_slots)
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
            "aster.story",
            "fallback.apply",
            "Re-ran dialogue-anchor selection after anchored Segments moved",
            moved_anchor_slot_ids=sorted(moved_anchor_slot_ids),
            changed_anchor_slot_ids=sorted(changed_anchor_slot_ids),
            reset_slot_ids=sorted(reset_slot_ids),
        )
        return refreshed_slots, reset_slot_ids

    def anchor_story(
        self,
        slots: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return self.story_editor.anchor(slots)

    def validate_composition(
        self,
        slots: list[dict[str, Any]],
        candidate_pool: dict[str, list[dict[str, Any]]],
    ) -> None:
        self.edit_composer.validate(slots, candidate_pool)

    def compose(
        self,
        slots: list[dict[str, Any]],
        candidate_pool: dict[str, list[dict[str, Any]]],
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, Any],
        dict[str, dict[str, Any]],
    ]:
        return self.edit_composer.compose(slots, candidate_pool)

    def build_script(
        self,
        slots: list[dict[str, Any]],
        selected_path: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return self.edit_composer.build_script(slots, selected_path)

    def revise(
        self,
        slots: list[dict[str, Any]],
        candidate_pool: dict[str, list[dict[str, Any]]],
        script: list[dict[str, Any]],
        pairwise_scores: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return self.revision_editor.revise(
            slots,
            candidate_pool,
            script,
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
        feedback = merge_planning_feedback(
            self.context.get_artifact("planning_feedback"),
            attempt=attempt,
            error=error,
            diagnostics=diagnostics,
            failed_slots=failed_slots,
            candidates_per_slot=self.config.candidate_retrieval.candidates_per_slot,
        )
        self.context.set_artifact("planning_feedback", feedback)


__all__ = [
    "ASTERTeam",
    "NoFeasiblePathError",
]
