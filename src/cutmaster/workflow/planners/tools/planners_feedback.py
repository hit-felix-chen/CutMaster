from __future__ import annotations

import json
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


def _merge_candidate_failure_evidence(
    previous: dict[str, Any],
    diagnostics: dict[str, Any],
    failed_slots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Retain distinct failed windows and requirements as soft diagnostic evidence."""

    by_slot_id = {
        str(slot["slot_id"]): slot
        for slot in failed_slots
        if slot.get("slot_id")
    }

    def evidence_key(item: dict[str, Any]) -> str:
        return json.dumps(
            {
                name: item.get(name)
                for name in (
                    "slot_ids",
                    "source_segment_id",
                    "reason_code",
                    "timestamp",
                    "planned_content_description",
                    "required_visible_subjects",
                    "planned_duration_ms",
                )
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    evidence_by_key: dict[str, dict[str, Any]] = {}
    for item in previous.get("candidate_failure_evidence") or []:
        evidence_by_key[evidence_key(item)] = dict(item)
    represented_groups: set[str] = set()

    def visit(items: list[dict[str, Any]], inherited_group_id: str = "") -> None:
        for item in items:
            group_id = str(item.get("group_id") or inherited_group_id)
            children = item.get("candidate_rejections")
            if isinstance(children, list) and children:
                visit(children, group_id)
                continue
            reason_code = str(item.get("reason_code") or "")
            if not reason_code:
                continue
            slot_id = str(item.get("slot_id") or "")
            if slot_id:
                scoped_slots = (
                    [by_slot_id[slot_id]] if slot_id in by_slot_id else []
                )
            else:
                scoped_slots = [
                    slot
                    for slot in failed_slots
                    if group_id and str(slot.get("group_id") or "") == group_id
                ]
            # candidate_rejections can also contain rejected alternatives from
            # healthy groups. They must not contaminate a failed group's repair.
            if not scoped_slots:
                continue
            represented_groups.update(
                str(slot.get("group_id") or "") for slot in scoped_slots
            )
            slot_ids = tuple(sorted(str(slot["slot_id"]) for slot in scoped_slots))
            source_segment_id = str(
                item.get("source_segment_id")
                or scoped_slots[0].get("source_segment_id")
                or ""
            )
            compact: dict[str, Any] = {
                "slot_ids": list(slot_ids),
                "source_segment_id": source_segment_id,
                "reason_code": reason_code,
            }
            if len(scoped_slots) == 1:
                original_slot = scoped_slots[0]
                if original_slot.get("content_description") is not None:
                    compact["planned_content_description"] = str(
                        original_slot["content_description"]
                    )
                if isinstance(original_slot.get("required_visible_subjects"), list):
                    compact["required_visible_subjects"] = list(
                        original_slot["required_visible_subjects"]
                    )
                if original_slot.get("planned_duration_ms") is not None:
                    compact["planned_duration_ms"] = original_slot["planned_duration_ms"]
            for name in (
                "diagnosis",
                "repair_requirement",
                "timestamp",
                "planned_content_description",
                "candidate_description",
                "visible_description",
                "visual_evidence",
                "planning_segment_id",
                "diagnostic_source",
            ):
                if item.get(name) is not None:
                    compact[name] = str(item[name])
            for name in (
                "kinetic_energy",
                "static_threshold",
                "protagonist_visibility_likert",
                "protagonist_visibility_likert_threshold",
                "planned_duration_ms",
            ):
                if name in item:
                    compact[name] = item[name]
            for name in (
                "visible_subjects",
                "required_visible_subjects",
                "source_shot_ids",
            ):
                if isinstance(item.get(name), list):
                    compact[name] = [str(value) for value in item[name]]
            key = evidence_key(compact)
            previous_item = evidence_by_key.pop(key, {})
            compact["occurrences"] = int(previous_item.get("occurrences", 0)) + 1
            evidence_by_key[key] = compact

    rejections = diagnostics.get("candidate_rejections")
    if isinstance(rejections, list) and rejections:
        visit(rejections)
    else:
        for group_id, items in (diagnostics.get("group_failures") or {}).items():
            visit(items, str(group_id))
    empty_groups = {
        str(group_id)
        for group_id in diagnostics.get("semantic_zero_candidate_group_ids") or []
    } - represented_groups
    # A normally returned empty batch has no sampled visual evidence to invent.
    # Preserve its original requirements so a changed contract can reconsider it.
    for slot in failed_slots:
        if str(slot.get("group_id") or "") not in empty_groups:
            continue
        visit(
            [
                {
                    "group_id": str(slot.get("group_id") or ""),
                    "slot_id": str(slot.get("slot_id") or ""),
                    "reason_code": str(
                        diagnostics.get("reason_code")
                        or "candidate_trajectory_unavailable"
                    ),
                    "diagnosis": str(
                        diagnostics.get("diagnosis")
                        or "No complete trajectory was returned."
                    ),
                    "diagnostic_source": "empty_candidate_batch",
                    **(
                        {"repair_requirement": diagnostics["repair_requirement"]}
                        if diagnostics.get("repair_requirement") is not None
                        else {}
                    ),
                }
            ]
        )
    # Keep repeated examples once across retries/checkpoints, without turning
    # historical windows or requirements into permanent source exclusions.
    return list(evidence_by_key.values())[-128:]


def merge_planners_feedback(
    previous: dict[str, Any] | None,
    *,
    attempt: int,
    error: str,
    diagnostics: dict[str, Any],
    failed_slots: list[dict[str, Any]],
) -> dict[str, Any]:
    previous = previous or {}
    compact_diagnostic_keys = {
        "reason_code",
        "diagnosis",
        "repair_requirement",
        "failure_stage",
        "error_type",
        "failed_group_ids",
        "failed_parent_group_ids",
        "failed_slot_ids",
        "semantic_zero_candidate_group_ids",
        "unavailable_source_segment_ids",
        "valid_trajectory_counts",
        "viable_trajectory_counts",
        "shortages",
        "aster_attempt",
        "replan_scope",
        "local_replan_attempt",
    }
    compact_diagnostics = {
        key: value
        for key, value in diagnostics.items()
        if key in compact_diagnostic_keys
    }
    accumulated: dict[str, dict[str, Any]] = {}
    unassigned: list[dict[str, Any]] = []
    for failed in [*(previous.get("failed_slots") or []), *failed_slots]:
        slot_id = str(failed.get("slot_id") or "")
        if slot_id:
            accumulated[slot_id] = failed
        else:
            unassigned.append(failed)

    failure_history = list(previous.get("failure_history") or [])[-4:]
    failure_history.append(
        {
            "attempt": attempt,
            "error": error,
            "failure_stage": diagnostics.get("failure_stage"),
            "reason_code": diagnostics.get("reason_code"),
            "failed_group_ids": list(
                diagnostics.get("failed_group_ids") or []
            ),
            "failed_slot_ids": list(
                diagnostics.get("failed_slot_ids") or []
            ),
        }
    )
    unavailable_source_segment_ids = {
        str(value)
        for value in previous.get("unavailable_source_segment_ids") or []
        if str(value)
    }
    unavailable_source_segment_ids.update(
        str(value)
        for value in diagnostics.get("unavailable_source_segment_ids") or []
        if str(value)
    )
    return {
        "attempt": attempt,
        "error": error,
        "diagnostics": compact_diagnostics,
        "reason_code": diagnostics.get("reason_code"),
        "diagnosis": diagnostics.get("diagnosis"),
        "repair_requirement": diagnostics.get("repair_requirement"),
        "failure_stage": diagnostics.get("failure_stage"),
        "failed_slots": [*accumulated.values(), *unassigned],
        "candidate_failure_evidence": _merge_candidate_failure_evidence(
            previous,
            diagnostics,
            failed_slots,
        ),
        "unavailable_source_segment_ids": sorted(
            unavailable_source_segment_ids
        ),
        "failure_history": failure_history,
        "instruction": str(
            diagnostics.get("repair_requirement")
            or "Redesign each failed complete Slot Group with supported source evidence. "
            "Keep every group atomic and preserve strict source order between groups."
        ),
    }


__all__ = ["build_stage_failure_diagnostics", "merge_planners_feedback"]
