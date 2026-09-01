from __future__ import annotations

import re
from typing import Any

from cutmaster.infrastructure.observability.logging import error_summary


def _root_error(error: BaseException) -> BaseException:
    current = error
    seen: set[int] = set()
    while current.__cause__ is not None and id(current) not in seen:
        seen.add(id(current))
        current = current.__cause__
    return current


def _stage_repair(stage: str, message: str) -> tuple[str, str]:
    lowered = message.lower()
    if stage == "slot_arrangement":
        if "duration" in lowered or "capacity" in lowered or "available=" in lowered:
            return (
                "arrangement_capacity_or_duration",
                "Rebalance Slot durations and move the complete affected group to a Segment "
                "with enough capacity; keep group order and the requested total duration.",
            )
        if "increasing" in lowered or "source order" in lowered or "chronolog" in lowered:
            return (
                "arrangement_source_order",
                "Reassign complete Slot Groups so their Segment IDs are strictly increasing; "
                "never move a later group back to an earlier Segment.",
            )
        return (
            "arrangement_response_invalid",
            "Regenerate the Arrangement and correct every reported Slot, grouping, duration, "
            "Segment-capacity, and Segment-order violation.",
        )
    if stage == "dialogue_anchor_selection":
        if "l-cut" in lowered and "overlap" in lowered:
            return (
                "anchor_audio_overlap",
                "Choose fewer or shorter Anchors whose L-cut output-audio ranges do not overlap.",
            )
        if "infeasible child group" in lowered or "capacity" in lowered:
            return (
                "anchor_partition_capacity",
                "Move the Anchor inside the Segment so the ordinary Slots before and after it "
                "still fit, or reassign the complete parent group to a roomier Segment.",
            )
        if "segment bounds" in lowered or "output timeline" in lowered:
            return (
                "anchor_window_out_of_bounds",
                "Choose a dialogue passage whose source picture and L-cut audio both fit inside "
                "the assigned Segment and the output timeline.",
            )
        if "strictly increasing" in lowered or "source order" in lowered:
            return (
                "anchor_source_order",
                "Choose Anchor picture windows in Slot order with strictly increasing, "
                "non-overlapping source ranges.",
            )
        if "no eligible" in lowered or "at least one" in lowered:
            return (
                "anchor_unavailable_in_arrangement",
                "Reassign at least one suitable Slot Group to a dialogue Segment that contains "
                "a coherent passage meeting the duration and capacity limits.",
            )
        return (
            "anchor_response_invalid",
            "Select a smaller valid Anchor set and correct every reported dialogue, timing, "
            "source-order, overlap, and partition-capacity violation.",
        )
    if stage == "retrieval":
        return (
            "candidate_trajectory_unavailable",
            "Use the recorded visual rejections to revise the whole failed group, then retrieve "
            "at least one ordered, non-overlapping complete trajectory.",
        )
    return (
        "complete_path_unavailable",
        "Reassign the failed complete group or retrieve alternatives that preserve source order "
        "and do not overlap with both the previous and following groups.",
    )


def build_stage_failure_diagnostics(
    *,
    stage: str,
    error: BaseException,
    slots: list[dict[str, Any]],
    existing: dict[str, Any] | None = None,
    previous: dict[str, Any] | None = None,
    preserve_previous_groups: bool = False,
) -> dict[str, Any]:
    """Normalize one failed ASTER stage into feedback for the next round."""

    root = _root_error(error)
    message = error_summary(root)
    reason_code, repair_requirement = _stage_repair(stage, message)
    diagnostics = dict(existing or {})
    diagnostics.setdefault("reason_code", reason_code)
    diagnostics.setdefault("diagnosis", f"{stage} failed: {message}")
    diagnostics.setdefault("repair_requirement", repair_requirement)
    diagnostics["failure_stage"] = stage
    diagnostics["error_type"] = type(root).__name__

    failed_group_ids = {
        str(value)
        for value in diagnostics.get("failed_group_ids") or []
        if str(value)
    }
    failed_group_id = str(diagnostics.get("failed_group_id") or "")
    if failed_group_id:
        failed_group_ids.add(failed_group_id)
    failed_group_ids.update(re.findall(r"\bgroup_[A-Za-z0-9_]+\b", message))

    failed_slot_ids = {
        str(value)
        for value in diagnostics.get("failed_slot_ids") or []
        if str(value)
    }
    failed_slot_id = str(diagnostics.get("failed_slot_id") or "")
    if failed_slot_id:
        failed_slot_ids.add(failed_slot_id)
    failed_slot_ids.update(re.findall(r"\bslot_[A-Za-z0-9_]+\b", message))

    if (stage == "slot_arrangement" or preserve_previous_groups) and previous:
        failed_group_ids.update(
            str(value)
            for value in previous.get("failed_group_ids") or []
            if str(value)
        )
        failed_slot_ids.update(
            str(value)
            for value in previous.get("failed_slot_ids") or []
            if str(value)
        )
    failed_parent_group_ids = {
        str(value)
        for value in diagnostics.get("failed_parent_group_ids") or []
        if str(value)
    }
    if (stage == "slot_arrangement" or preserve_previous_groups) and previous:
        failed_parent_group_ids.update(
            str(value)
            for value in previous.get("failed_parent_group_ids") or []
            if str(value)
        )
    for slot in slots:
        slot_id = str(slot.get("slot_id") or "")
        group_id = str(slot.get("group_id") or "")
        parent_group_id = str(slot.get("parent_group_id") or group_id)
        if slot_id in failed_slot_ids or group_id in failed_group_ids:
            if group_id:
                failed_group_ids.add(group_id)
            if parent_group_id:
                failed_parent_group_ids.add(parent_group_id)

    diagnostics["failed_group_ids"] = sorted(failed_group_ids)
    diagnostics["failed_parent_group_ids"] = sorted(failed_parent_group_ids)
    diagnostics["failed_slot_ids"] = sorted(failed_slot_ids)
    return diagnostics


def merge_planners_feedback(
    previous: dict[str, Any] | None,
    *,
    attempt: int,
    error: str,
    diagnostics: dict[str, Any],
    failed_slots: list[dict[str, Any]],
    target_trajectories_per_group: int,
) -> dict[str, Any]:
    previous = previous or {}
    accumulated: dict[str, dict[str, Any]] = {}
    unassigned: list[dict[str, Any]] = []
    for failed in [*(previous.get("failed_slots") or []), *failed_slots]:
        slot_id = str(failed.get("slot_id") or "")
        if slot_id:
            accumulated[slot_id] = failed
        else:
            unassigned.append(failed)

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
        "reason_code": diagnostics.get("reason_code"),
        "diagnosis": diagnostics.get("diagnosis"),
        "repair_requirement": diagnostics.get("repair_requirement"),
        "failure_stage": diagnostics.get("failure_stage"),
        "current_failed_slots": failed_slots,
        "failed_slots": [*accumulated.values(), *unassigned],
        "target_trajectories_per_group": target_trajectories_per_group,
        "failure_history": failure_history,
        "instruction": str(
            diagnostics.get("repair_requirement")
            or "Redesign each failed complete Slot Group with supported source evidence. "
            "Keep every group atomic and preserve strict source order between groups."
        ),
    }


__all__ = ["build_stage_failure_diagnostics", "merge_planners_feedback"]
