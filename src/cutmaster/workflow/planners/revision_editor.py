from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import AppConfig, LLMConfig
from cutmaster.workflow.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.workflow.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)
from cutmaster.workflow.prompting.planners import ScriptReviewDetails
from cutmaster.workflow.shared.execution_context import WorkflowContext
from cutmaster.workflow.planners.edit_composer import _score_candidate_path, path_to_script
from cutmaster.workflow.shared.timecode import parse_range

def _validate_patches(
    parsed: dict[str, Any],
    pool: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    valid = {
        group_id: {
            str(trajectory["trajectory_id"])
            for trajectory in trajectories
        }
        for group_id, trajectories in pool.items()
    }
    patches = parsed["patches"]
    if not isinstance(patches, list):
        raise ValueError("patches must be an array")
    normalized: list[dict[str, Any]] = []
    patched_groups: set[str] = set()
    for patch in patches:
        operation = str(patch["operation"]).lower()
        group_id = str(patch["group_id"])
        if operation == "keep":
            continue
        if operation != "replace" or group_id not in valid:
            raise ValueError(f"Unsupported patch: {patch}")
        trajectory_id = str(patch["trajectory_id"])
        if trajectory_id not in valid[group_id]:
            raise ValueError(
                f"Invalid trajectory for {group_id}: {trajectory_id}"
            )
        if group_id in patched_groups:
            raise ValueError(f"Multiple replacements returned for {group_id}")
        patched_groups.add(group_id)
        normalized.append(
            {
                "operation": "replace",
                "group_id": group_id,
                "trajectory_id": trajectory_id,
                "reason": str(patch["reason"]),
            }
        )
    return normalized


def _script_sequence_issue(script: list[dict[str, Any]]) -> dict[str, Any] | None:
    previous_end = -1.0
    previous_slot = ""
    for item in script:
        start, end = parse_range(item["timestamp"])
        if start < previous_end:
            return build_prompt_failure(
                PromptFailureCode.SOURCE_CHRONOLOGY_OR_OVERLAP,
                previous_slot_id=previous_slot,
                slot_id=item["slot_id"],
                previous_end_sec=previous_end,
                current_start_sec=start,
            )
        previous_end = end
        previous_slot = item["slot_id"]
    return None


def review_and_patch(
    slots: list[dict[str, Any]],
    pool: dict[str, list[dict[str, Any]]],
    script: list[dict[str, Any]],
    config: LLMConfig,
    context: WorkflowContext,
    pairwise_scores: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    package = prompt_registry.build(
        PromptStage.PLANNERS,
        PromptTask.SCRIPT_REVIEW,
        ScriptReviewDetails(
            slots=slots,
            candidate_pool=pool,
        ),
    )
    patches = context.call_prompt(
        package=package,
        config=config,
        validate_business=lambda parsed: _validate_patches(parsed, pool),
    )
    original_by_slot = {item["slot_id"]: item for item in script}
    slots_by_group: dict[str, list[dict[str, Any]]] = {}
    for slot in slots:
        slots_by_group.setdefault(str(slot["group_id"]), []).append(slot)
    trajectories = {
        str(trajectory["trajectory_id"]): trajectory
        for values in pool.values()
        for trajectory in values
    }
    candidates = {
        candidate["candidate_id"]: candidate
        for values in pool.values()
        for trajectory in values
        for candidate in trajectory["items"]
    }
    candidates.update(
        {
            str(slot["fixed_candidate"]["candidate_id"]): slot["fixed_candidate"]
            for slot in slots
            if slot.get("fixed_candidate") is not None
        }
    )
    replacements: dict[str, dict[str, dict[str, Any]]] = {}
    for patch in patches:
        group_id = patch["group_id"]
        trajectory = trajectories[patch["trajectory_id"]]
        group_slots = slots_by_group[group_id]
        trajectory_items = []
        for raw in trajectory["items"]:
            candidate = dict(raw)
            candidate["group_id"] = group_id
            candidate["trajectory_id"] = trajectory["trajectory_id"]
            trajectory_items.append(candidate)
        replacement_items = path_to_script(
            group_slots,
            trajectory_items,
            Path(script[0]["video_name"]),
        )
        replacements[group_id] = {}
        for replacement in replacement_items:
            slot_id = str(replacement["slot_id"])
            original = original_by_slot[slot_id]
            replacement["_id"] = original["_id"]
            replacement["video_name"] = original["video_name"]
            replacement["narration"] = original["narration"]
            replacements[group_id][slot_id] = replacement

    def apply_subset(subset: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
        chosen = {patch["group_id"] for patch in subset}
        return [
            replacements[str(slot["group_id"])][str(slot["slot_id"])]
            if str(slot["group_id"]) in chosen
            else original_by_slot[slot["slot_id"]]
            for slot in slots
        ]

    accepted: list[dict[str, Any]] = []
    patched = script
    baseline_score = _score_candidate_path(
        slots,
        [candidates[item["candidate_id"]] for item in script],
        pairwise_scores,
    )
    for size in range(len(patches), -1, -1):
        feasible: list[
            tuple[list[dict[str, Any]], tuple[dict[str, Any], ...], float]
        ] = []
        for subset in itertools.combinations(patches, size):
            proposal = apply_subset(subset)
            if _script_sequence_issue(proposal) is not None:
                continue
            proposal_score = _score_candidate_path(
                slots,
                [candidates[item["candidate_id"]] for item in proposal],
                pairwise_scores,
            )
            if proposal_score + 1e-9 >= baseline_score:
                feasible.append((proposal, subset, proposal_score))
        if feasible:
            patched, subset, _ = max(
                feasible,
                key=lambda value: value[2],
            )
            accepted = list(subset)
            break
    accepted_groups = {patch["group_id"] for patch in accepted}
    rejected_patches: list[dict[str, Any]] = []
    for patch in patches:
        group_id = patch["group_id"]
        if group_id in accepted_groups:
            continue
        trial = [
            replacements[group_id][str(slot["slot_id"])]
            if str(slot["group_id"]) == group_id
            else item
            for slot, item in zip(slots, patched, strict=True)
        ]
        issue = _script_sequence_issue(trial)
        trial_score = (
            _score_candidate_path(
                slots,
                [candidates[item["candidate_id"]] for item in trial],
                pairwise_scores,
            )
            if issue is None
            else None
        )
        rejection = (
            issue
            if issue is not None
            else build_prompt_failure(
                (
                    PromptFailureCode.PATCH_DEGRADES_OR_REQUIRES_UNSCORED_PATH
                    if (
                        trial_score is not None
                        and trial_score + 1e-9 < baseline_score
                    )
                    else PromptFailureCode.PATCH_EXCLUDED_BY_MAXIMAL_FEASIBLE_SUBSET
                ),
                group_id=group_id,
            )
        )
        rejected_patches.append(
            {
                **patch,
                "rejection": rejection,
                "trial_path_score": (
                    round(trial_score, 6) if trial_score is not None else None
                ),
            }
        )
    context.set_artifact(
        "latest_patch_evaluation",
        {"accepted": accepted, "rejected": rejected_patches},
    )
    context.record_script_version(
        patched,
        source="llm_patch",
        patches=accepted,
        rejected_patches=rejected_patches,
    )
    return patched, accepted


class RevisionEditorAgent:
    """R agent: review and revise a composition within its candidate space."""

    def __init__(self, config: AppConfig, context: WorkflowContext) -> None:
        self.config = config
        self.context = context

    def revise(
        self,
        slots: list[dict[str, Any]],
        candidate_space: dict[str, list[dict[str, Any]]],
        script: list[dict[str, Any]],
        pairwise_scores: dict[str, dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return review_and_patch(
            slots,
            candidate_space,
            script,
            self.config.llm,
            self.context,
            pairwise_scores,
        )


__all__ = ["RevisionEditorAgent"]
