from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import LLMConfig
from cutmaster.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.prompting.planner import ScriptReviewDetails
from cutmaster.runtime.workflow_context import WorkflowContext
from cutmaster.planner.sequence_selection import _score_candidate_path, path_to_script
from cutmaster.timecode import parse_range

def _validate_patches(
    parsed: dict[str, Any],
    slots: list[dict[str, Any]],
    pool: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    valid = {
        slot["slot_id"]: {item["candidate_id"] for item in pool[slot["slot_id"]]}
        for slot in slots
    }
    patches = parsed["patches"]
    if not isinstance(patches, list):
        raise ValueError("patches must be an array")
    normalized: list[dict[str, Any]] = []
    patched_slots: set[str] = set()
    for patch in patches:
        operation = str(patch["operation"]).lower()
        slot_id = str(patch["slot_id"])
        if operation == "keep":
            continue
        if operation != "replace" or slot_id not in valid:
            raise ValueError(f"Unsupported patch: {patch}")
        candidate_id = str(patch["candidate_id"])
        if candidate_id not in valid[slot_id]:
            raise ValueError(f"Invalid candidate for {slot_id}: {candidate_id}")
        if slot_id in patched_slots:
            raise ValueError(f"Multiple replacements returned for {slot_id}")
        patched_slots.add(slot_id)
        normalized.append(
            {
                "operation": "replace",
                "slot_id": slot_id,
                "candidate_id": candidate_id,
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
            return {
                "reason": "source_chronology_or_overlap",
                "previous_slot_id": previous_slot,
                "slot_id": item["slot_id"],
                "previous_end_sec": previous_end,
                "current_start_sec": start,
            }
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
        PromptStage.PLANNER,
        PromptTask.SCRIPT_REVIEW,
        ScriptReviewDetails(
            slots=slots,
            candidate_pool=pool,
        ),
    )
    patches = context.call_prompt(
        package=package,
        config=config,
        validate_business=lambda parsed: _validate_patches(parsed, slots, pool),
    )
    original_by_slot = {item["slot_id"]: item for item in script}
    candidates = {
        candidate["candidate_id"]: candidate
        for values in pool.values()
        for candidate in values
    }
    replacements: dict[str, dict[str, Any]] = {}
    for patch in patches:
        slot_id = patch["slot_id"]
        slot = next(value for value in slots if value["slot_id"] == slot_id)
        replacement = path_to_script([slot], [candidates[patch["candidate_id"]]], Path(script[0]["video_name"]))[0]
        replacement["_id"] = original_by_slot[slot_id]["_id"]
        replacement["video_name"] = original_by_slot[slot_id]["video_name"]
        replacement["narration"] = original_by_slot[slot_id]["narration"]
        replacements[slot_id] = replacement

    def apply_subset(subset: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
        chosen = {patch["slot_id"] for patch in subset}
        return [
            replacements[slot["slot_id"]]
            if slot["slot_id"] in chosen
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
    accepted_slots = {patch["slot_id"] for patch in accepted}
    rejected_patches: list[dict[str, Any]] = []
    for patch in patches:
        if patch["slot_id"] in accepted_slots:
            continue
        trial = list(patched)
        slot_index = next(
            index
            for index, slot in enumerate(slots)
            if slot["slot_id"] == patch["slot_id"]
        )
        trial[slot_index] = replacements[patch["slot_id"]]
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
        rejected_patches.append(
            {
                **patch,
                "rejection_reason": (
                    issue["reason"]
                    if issue
                    else (
                        "degrades_or_requires_unscored_hard_cut_path"
                        if (
                            trial_score is not None
                            and trial_score + 1e-9 < baseline_score
                        )
                        else "excluded_by_maximal_feasible_patch_subset"
                    )
                ),
                "conflict": issue,
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
