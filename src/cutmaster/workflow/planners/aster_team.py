from __future__ import annotations

from pathlib import Path
from typing import Any

from cutmaster.workflow.planners.timeline_scout import (
    TimelineScoutAgent,
    _planning_units,
    _range_ms,
    _source_segment_context,
    _viable_trajectory_counts,
)
from cutmaster.workflow.planners.story_editor import StoryEditorAgent
from cutmaster.workflow.planners.tools.segment_media import SegmentMediaReader
from cutmaster.configuration.schema import AppConfig
from cutmaster.workflow.contracts.planners import PlannersRequest
from cutmaster.workflow.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)
from cutmaster.infrastructure.observability.logging import log_event
from cutmaster.workflow.shared.execution_context import WorkflowContext
from cutmaster.workflow.ports import CancellationToken
from cutmaster.workflow.planners.revision_editor import RevisionEditorAgent
from cutmaster.workflow.planners.edit_composer import (
    EditComposerAgent,
    _trajectory_units,
)
from cutmaster.workflow.planners.tools.errors import (
    GroupNoCandidateError,
    NoFeasiblePathError,
)
from cutmaster.workflow.planners.tools.planners_feedback import merge_planners_feedback
from cutmaster.workflow.planners.arrangement_architect import (
    ArrangementArchitectAgent,
)


def _planning_contracts(
    groups: list[dict[str, Any]],
    segments: list[dict[str, Any]],
    slots: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    segments_by_id = {
        str(segment.get("planning_segment_id") or ""): dict(segment)
        for segment in segments
        if str(segment.get("planning_segment_id") or "")
    }
    contracts: dict[str, dict[str, Any]] = {}
    slots_by_id = {str(slot.get("slot_id") or ""): dict(slot) for slot in slots}
    for group in groups:
        group_id = str(group.get("group_id") or "")
        planning_segment_id = str(group.get("planning_segment_id") or "")
        segment = segments_by_id.get(planning_segment_id)
        if not group_id or segment is None:
            continue
        contracts[group_id] = {
            "group": dict(group),
            "planning_segment": segment,
            "slots": [
                slots_by_id.get(str(slot_id))
                for slot_id in group.get("slot_ids") or []
            ],
        }
    return contracts


def _candidate_trajectories_match(
    group_id: str,
    trajectories: Any,
    contract: dict[str, Any],
) -> bool:
    if not isinstance(trajectories, list) or not trajectories:
        return False
    group = contract["group"]
    planning_segment = contract["planning_segment"]
    slots = contract["slots"]
    if any(not isinstance(slot, dict) for slot in slots):
        return False
    expected_segment_id = str(group["planning_segment_id"])
    expected_slot_ids = [str(value) for value in group["slot_ids"]]
    for trajectory in trajectories:
        if not isinstance(trajectory, dict):
            return False
        items = trajectory.get("items")
        if (
            str(trajectory.get("group_id") or "") != group_id
            or str(trajectory.get("planning_segment_id") or "")
            != expected_segment_id
            or not isinstance(items, list)
            or [str(item.get("slot_id") or "") for item in items]
            != expected_slot_ids
        ):
            return False
        previous_end_ms: int | None = None
        for slot, item in zip(slots, items, strict=True):
            try:
                start_ms, end_ms = _range_ms(str(item.get("timestamp") or ""))
            except (TypeError, ValueError):
                return False
            if (
                end_ms - start_ms != int(slot["planned_duration_ms"])
                or start_ms < int(planning_segment["start_ms"])
                or end_ms > int(planning_segment["end_ms"])
                or (previous_end_ms is not None and start_ms < previous_end_ms)
            ):
                return False
            previous_end_ms = end_ms
    try:
        _trajectory_units(
            slots,
            {group_id: trajectories},
            [planning_segment],
        )
    except (KeyError, NoFeasiblePathError, TypeError, ValueError):
        return False
    return True


class ASTERTeam:
    """Coordinate the five specialized ASTER agents."""

    def __init__(
        self,
        video_path: Path,
        segment_cache_directory: Path,
        config: AppConfig,
        context: WorkflowContext,
    ) -> None:
        self.config = config
        self.context = context
        video_description = context.get_artifact("video_description")
        if video_description is None:
            raise RuntimeError(
                "Video description must be available before ASTERTeam initialization"
            )
        media = SegmentMediaReader(
            video_path,
            segment_cache_directory,
            video_description,
        )
        self.arrangement_architect = ArrangementArchitectAgent(config, context)
        self.story_editor = StoryEditorAgent(config, context)
        self.edit_composer = EditComposerAgent(
            media,
            video_path,
            config,
            context,
        )
        self.revision_editor = RevisionEditorAgent(config, context)
        self.timeline_scout = TimelineScoutAgent(
            media,
            config,
            context,
        )

    def profile_music(
        self,
        music_memory: dict[str, Any],
        target_duration_sec: float,
        output_path: Path,
    ) -> dict[str, Any]:
        return self.arrangement_architect.profile_music(
            music_memory,
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
        failure = build_prompt_failure(
            PromptFailureCode.PROVIDER_DATA_INSPECTION_FAILED,
            missing_shot_count=len(missing),
        )
        log_event(
            "WARNING",
            "aster.arrangement",
            "fallback.apply",
            "ASTER is continuing with Shots that lack visual annotations",
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
            **failure,
        )

    def arrange(
        self,
        request: PlannersRequest,
        music_profile: dict[str, Any],
    ) -> list[dict[str, Any]]:
        self._warn_about_missing_shot_annotations()
        return self.arrangement_architect.arrange(request, music_profile)

    def scout(
        self,
        slots: list[dict[str, Any]],
        cancellation_token: CancellationToken | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        return self.timeline_scout.scout(slots, cancellation_token)

    def repair_groups(
        self,
        slots: list[dict[str, Any]],
        diagnostics: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], set[str]]:
        """Repair whole failed Arrangement groups before Story runs again."""

        failed_group_ids = {
            str(value)
            for value in diagnostics.get("failed_group_ids") or []
            if str(value)
        }
        failed_parent_group_ids = {
            str(value)
            for value in diagnostics.get("failed_parent_group_ids") or []
            if str(value)
        }
        arrangement_slots = self.story_editor.restore_arrangement_slots(slots)
        parent_by_runtime_group = {
            str(slot.get("group_id") or ""): str(
                slot.get("parent_group_id") or slot.get("group_id") or ""
            )
            for slot in slots
        }
        failed_parent_group_ids.update(
            parent_by_runtime_group[group_id]
            for group_id in failed_group_ids
            if group_id in parent_by_runtime_group
        )
        if not failed_parent_group_ids:
            raise ValueError("ASTER group repair requires at least one failed group")
        failures = [
            {
                "slot_id": str(slot["slot_id"]),
                "group_id": str(slot["group_id"]),
                "source_segment_id": str(slot["source_segment_id"]),
                "content_description": str(slot["content_description"]),
                "reason": diagnostics,
            }
            for slot in arrangement_slots
            if str(slot["group_id"]) in failed_parent_group_ids
        ]
        if not failures:
            raise ValueError(
                "ASTER group repair could not map failed groups to Arrangement Slots"
            )
        redesigned_slots, replanned_slot_ids = self.arrangement_architect.repair(
            arrangement_slots,
            failures,
        )
        log_event(
            "WARNING",
            "aster.arrangement",
            "fallback.apply",
            "Replanned complete Slot Groups; Story Editor will run next",
            failed_group_ids=sorted(failed_group_ids),
            failed_parent_group_ids=sorted(failed_parent_group_ids),
            replanned_slot_ids=sorted(replanned_slot_ids),
        )
        return redesigned_slots, replanned_slot_ids

    def refresh_story_groups(
        self,
        slots: list[dict[str, Any]],
        *,
        previous_slots: list[dict[str, Any]],
        replanned_slot_ids: set[str],
    ) -> tuple[list[dict[str, Any]], set[str], set[str]]:
        """Refresh Story for repaired Slots, retaining verified healthy groups."""

        refreshed_slots = self.story_editor.anchor_groups(
            slots,
            previous_slots=previous_slots,
            replanned_slot_ids=replanned_slot_ids,
        )
        refreshed_by_id = {
            str(slot["slot_id"]): slot for slot in refreshed_slots
        }
        missing = replanned_slot_ids - set(refreshed_by_id)
        if missing:
            raise ValueError(
                "Incremental Story repair omitted replanned Slots: "
                + ", ".join(sorted(missing))
            )
        affected_parent_group_ids = {
            str(
                refreshed_by_id[slot_id].get("parent_group_id")
                or refreshed_by_id[slot_id].get("group_id")
                or ""
            )
            for slot_id in replanned_slot_ids
        }
        affected_parent_group_ids.discard("")
        return refreshed_slots, replanned_slot_ids, affected_parent_group_ids

    def scout_with_reuse(
        self,
        slots: list[dict[str, Any]],
        *,
        previous_candidate_pool: dict[str, list[dict[str, Any]]],
        previous_slots: list[dict[str, Any]],
        previous_planning_groups: list[dict[str, Any]],
        previous_planning_segments: list[dict[str, Any]],
        affected_parent_group_ids: set[str],
        cancellation_token: CancellationToken | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        """Retrieve changed groups while retaining exact healthy trajectories."""

        self.validate_planning(slots)
        current_groups = list(self.context.get_artifact("planning_groups") or [])
        current_segments = list(
            self.context.get_artifact("planning_segments") or []
        )
        previous_contracts = _planning_contracts(
            previous_planning_groups,
            previous_planning_segments,
            previous_slots,
        )
        current_contracts = _planning_contracts(
            current_groups,
            current_segments,
            slots,
        )
        reusable: dict[str, list[dict[str, Any]]] = {}
        retrieve_group_ids: set[str] = set()
        for group in current_groups:
            group_id = str(group["group_id"])
            parent_group_id = str(group["parent_group_id"])
            current_contract = current_contracts.get(group_id)
            old_contract = previous_contracts.get(group_id)
            trajectories = previous_candidate_pool.get(group_id)
            if (
                parent_group_id not in affected_parent_group_ids
                and current_contract is not None
                and old_contract == current_contract
                and _candidate_trajectories_match(
                    group_id,
                    trajectories,
                    current_contract,
                )
            ):
                reusable[group_id] = [dict(item) for item in trajectories or []]
            else:
                retrieve_group_ids.add(group_id)

        retrieved: dict[str, list[dict[str, Any]]] = {}
        self.context.set_artifact("candidate_rejections", [])
        self.context.set_artifact("retrieval_summary", {})
        self.context.set_artifact("retrieval_failure", None)
        if retrieve_group_ids:
            initial_pool = {
                str(group["group_id"]): reusable.get(
                    str(group["group_id"]),
                    [],
                )
                for group in current_groups
            }
            # Timeline Scout receives the complete Story topology so Anchor
            # boundaries and chronology against reused groups remain visible.
            # Only changed groups issue new model and VLM requests.
            self.context.set_artifact("candidate_pool", initial_pool)
            try:
                retrieved = self.timeline_scout.scout(
                    slots,
                    cancellation_token,
                    target_group_ids=retrieve_group_ids,
                    seed_candidate_pool=reusable,
                )
            except Exception:
                partial = self.context.get_artifact("candidate_pool")
                if isinstance(partial, dict):
                    retrieved = {
                        str(group_id): list(trajectories)
                        for group_id, trajectories in partial.items()
                        if str(group_id) in retrieve_group_ids
                        and isinstance(trajectories, list)
                    }
                combined = {
                    group_id: reusable.get(group_id, retrieved.get(group_id, []))
                    for group_id in current_contracts
                }
                self.context.set_artifact("candidate_pool", combined)
                raise

        combined = {
            str(group["group_id"]): reusable.get(
                str(group["group_id"]),
                retrieved.get(str(group["group_id"]), []),
            )
            for group in current_groups
        }
        self.context.set_artifact("candidate_pool", combined)
        summary = dict(self.context.get_artifact("retrieval_summary") or {})
        target = (
            self.config.planners.candidate_retrieval.target_trajectories_per_group
        )
        viable_counts = _viable_trajectory_counts(slots, combined)
        summary.update(
            {
                "valid_trajectory_counts": {
                    group_id: len(values)
                    for group_id, values in combined.items()
                },
                "viable_trajectory_counts": viable_counts,
                "underfilled_group_ids": sorted(
                    group_id
                    for group_id, count in viable_counts.items()
                    if 0 < count < target
                ),
            }
        )
        self.context.set_artifact("retrieval_summary", summary)
        log_event(
            "INFO",
            "aster.timeline",
            "fallback.apply",
            "Reused unchanged candidate trajectories after group repair",
            reused_group_ids=sorted(reusable),
            retrieved_group_ids=sorted(retrieve_group_ids),
        )
        return combined

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
        self.validate_planning(slots)
        self.edit_composer.validate(slots, candidate_pool)

    def validate_planning(self, slots: list[dict[str, Any]]) -> None:
        """Revalidate Story artifacts before using a resumed checkpoint."""

        units = _planning_units(slots, self.context)
        video_description = self.context.get_artifact("video_description")
        if not isinstance(video_description, dict):
            raise RuntimeError(
                "Video description is required for planning validation"
            )
        for unit in units:
            _source_segment_context(unit["planning_segment"], video_description)

    def validate_edit_checkpoint(
        self,
        slots: list[dict[str, Any]],
        candidate_pool: dict[str, list[dict[str, Any]]],
        beam_path: list[dict[str, Any]],
        selection: dict[str, Any],
        pairwise_scores: dict[str, dict[str, Any]],
    ) -> None:
        self.validate_composition(slots, candidate_pool)
        selected_ids = selection.get("selected_trajectory_ids")
        if not isinstance(selected_ids, dict):
            raise ValueError("Edit checkpoint has no selected trajectory mapping")
        self.edit_composer.validate_selected_path(
            slots,
            candidate_pool,
            beam_path,
            selected_ids,
            pairwise_scores,
        )

    def validate_revision_checkpoint(
        self,
        slots: list[dict[str, Any]],
        candidate_pool: dict[str, list[dict[str, Any]]],
        beam_path: list[dict[str, Any]],
        selection: dict[str, Any],
        raw_script: list[dict[str, Any]],
        pairwise_scores: dict[str, dict[str, Any]],
    ) -> None:
        self.validate_composition(slots, candidate_pool)
        final_selected = selection.get("selected_trajectory_ids")
        if not isinstance(final_selected, dict):
            raise ValueError("Revision checkpoint has no final selection")
        composer_selected = selection.get(
            "composer_selected_trajectory_ids",
            final_selected,
        )
        if not isinstance(composer_selected, dict):
            raise ValueError("Revision checkpoint has no Composer selection")
        self.edit_composer.validate_selected_path(
            slots,
            candidate_pool,
            beam_path,
            composer_selected,
            pairwise_scores,
        )
        self.edit_composer.validate_script(
            slots,
            candidate_pool,
            raw_script,
            final_selected,
            pairwise_scores,
        )

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
        feedback = merge_planners_feedback(
            self.context.get_artifact("planners_feedback"),
            attempt=attempt,
            error=error,
            diagnostics=diagnostics,
            failed_slots=failed_slots,
            target_trajectories_per_group=(
                self.config.planners.candidate_retrieval.target_trajectories_per_group
            ),
        )
        self.context.set_artifact("planners_feedback", feedback)


__all__ = [
    "ASTERTeam",
    "GroupNoCandidateError",
    "NoFeasiblePathError",
]
