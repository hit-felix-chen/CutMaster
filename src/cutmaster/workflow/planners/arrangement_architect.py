from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import AppConfig, LLMConfig
from cutmaster.workflow.contracts.planners import PlannersRequest
from cutmaster.workflow.planners.tools.music_analysis import (
    build_music_profile,
    compact_music_profile,
    write_music_profile,
)
from cutmaster.workflow.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.workflow.prompting.planners import SlotArrangementDetails
from cutmaster.infrastructure.observability.logging import log_event
from cutmaster.workflow.shared.execution_context import WorkflowContext

MIN_SLOT_DURATION_SEC = 1.5
MAX_DURATION_TOTAL_ERROR_SEC = 0.5
MAX_MUSIC_BOUNDARY_SHIFT_SEC = 0.75
MAX_TARGET_DURATION_RATIO = 1.5
MAX_AVERAGE_TARGET_ERROR_RATIO = 0.125
DURATION_TOLERANCE_SEC = 1e-6


def _milliseconds(seconds: float) -> int:
    """Convert a public second value to the workflow's millisecond timebase."""

    return int(round(float(seconds) * 1000.0))


def _segment_metadata(
    video_description: dict[str, Any],
) -> tuple[dict[str, int], dict[str, int]]:
    segment_order: dict[str, int] = {}
    segment_duration_ms: dict[str, int] = {}
    for index, segment in enumerate(video_description["segments"]):
        segment_id = str(segment["segment_id"])
        time_range = segment["time_range"]
        start_ms = _milliseconds(float(time_range["start_sec"]))
        end_ms = _milliseconds(float(time_range["end_sec"]))
        if end_ms <= start_ms:
            raise ValueError(f"Source Segment {segment_id} has no positive duration")
        segment_order[segment_id] = index
        segment_duration_ms[segment_id] = end_ms - start_ms
    return segment_order, segment_duration_ms


def _assign_source_groups(
    slots: list[dict[str, Any]],
    video_description: dict[str, Any],
) -> list[dict[str, Any]]:
    """Canonicalize maximal same-Segment runs into deterministic groups."""

    segment_order, _ = _segment_metadata(video_description)
    grouped: list[dict[str, Any]] = []
    previous_segment_id: str | None = None
    previous_group_segment_index = -1
    group_number = 0
    for slot in slots:
        segment_id = str(slot.get("source_segment_id") or "").strip()
        if segment_id not in segment_order:
            raise ValueError(
                f"Slot {slot.get('slot_id', '<unknown>')} must reference one valid "
                "source_segment_id"
            )
        if segment_id != previous_segment_id:
            segment_index = segment_order[segment_id]
            if segment_index <= previous_group_segment_index:
                raise ValueError(
                    "Source Group Segments must be in strictly increasing source order"
                )
            previous_group_segment_index = segment_index
            previous_segment_id = segment_id
            group_number += 1
        item = dict(slot)
        item["source_segment_id"] = segment_id
        item["group_id"] = f"group_{group_number:03d}"
        grouped.append(item)
    return grouped


def _arrangement_groups(slots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for slot in slots:
        group_id = str(slot["group_id"])
        segment_id = str(slot["source_segment_id"])
        if not groups or groups[-1]["group_id"] != group_id:
            groups.append(
                {
                    "group_id": group_id,
                    "slot_ids": [str(slot["slot_id"])],
                    "source_segment_id": segment_id,
                }
            )
            continue
        if groups[-1]["source_segment_id"] != segment_id:
            raise ValueError(f"Source Group {group_id} spans multiple Segments")
        groups[-1]["slot_ids"].append(str(slot["slot_id"]))
    return groups


def _validate_group_capacity(
    slots: list[dict[str, Any]],
    video_description: dict[str, Any],
) -> None:
    """Require room for one ordered, non-overlapping trajectory per group."""

    _, segment_duration_ms = _segment_metadata(video_description)
    required_by_group: dict[str, int] = {}
    segment_by_group: dict[str, str] = {}
    for slot in slots:
        group_id = str(slot["group_id"])
        segment_id = str(slot["source_segment_id"])
        planned_duration_ms = slot.get("planned_duration_ms")
        if (
            not isinstance(planned_duration_ms, int)
            or isinstance(planned_duration_ms, bool)
            or planned_duration_ms <= 0
        ):
            raise ValueError(
                f"Slot {slot['slot_id']} must have a positive planned_duration_ms"
            )
        previous_segment = segment_by_group.setdefault(group_id, segment_id)
        if previous_segment != segment_id:
            raise ValueError(f"Source Group {group_id} spans multiple Segments")
        required_by_group[group_id] = (
            required_by_group.get(group_id, 0) + planned_duration_ms
        )
    for group_id, required_ms in required_by_group.items():
        segment_id = segment_by_group[group_id]
        available_ms = segment_duration_ms[segment_id]
        if required_ms > available_ms:
            raise ValueError(
                f"Source Group {group_id} exceeds Segment {segment_id} capacity: "
                f"available={available_ms}ms required={required_ms}ms"
            )


def _request_metadata(request: PlannersRequest) -> dict[str, Any]:
    return {
        "instruction": request.prompt,
        "prompt_type": request.prompt_type,
        "video_title": request.video_title or request.video_path.stem,
        "target_duration_sec": request.target_output_length_sec,
    }


def _source_story_context(
    video_description: dict[str, Any],
    video_summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source": video_description["source"],
        "story_summary": video_summary,
        "segments": [
            {
                "segment_id": segment["segment_id"],
                "time_range": segment["time_range"],
                "has_dialogue": segment["has_dialogue"],
                "speech_mode": segment["speech_mode"],
                "content_type": segment["content_type"],
                "timeline_role": segment["timeline_role"],
                "segment_summary": segment["segment_summary"],
                "narrative_function": segment["narrative_function"],
                "emotional_tone": segment["emotional_tone"],
                "emotional_intensity": segment["emotional_intensity"],
                "appearing_characters": segment["appearing_characters"],
            }
            for segment in video_description["segments"]
        ],
    }


def _average_target_tolerance(
    target_duration_sec: float,
    target_clip_duration_sec: float,
) -> float:
    maximum_slot_count = max(
        1,
        int(target_duration_sec // MIN_SLOT_DURATION_SEC),
    )
    nearest_integer_count_error = min(
        abs(
            target_duration_sec / slot_count
            - target_clip_duration_sec
        )
        for slot_count in range(1, maximum_slot_count + 1)
    )
    return max(
        target_clip_duration_sec * MAX_AVERAGE_TARGET_ERROR_RATIO,
        nearest_integer_count_error + DURATION_TOLERANCE_SEC,
    )


def _validate_slots(
    parsed: dict[str, Any],
    target_duration_sec: float,
    target_clip_duration_sec: float,
    video_description: dict[str, Any],
) -> list[dict[str, Any]]:
    raw = parsed.get("slots")
    if not isinstance(raw, list) or not raw:
        raise ValueError("Expected at least one edit slot")
    slots: list[dict[str, Any]] = []
    segment_order, _ = _segment_metadata(video_description)
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Slot {index} must be an object")
        description = str(item.get("content_description") or "").strip()
        if not description:
            raise ValueError(f"Slot {index} has no content description")
        duration = float(item.get("desired_duration_sec") or 0)
        if duration < MIN_SLOT_DURATION_SEC:
            raise ValueError(
                f"Slot {index} desired duration must be at least "
                f"{MIN_SLOT_DURATION_SEC:.1f} seconds"
            )
        maximum_duration = (
            target_clip_duration_sec * MAX_TARGET_DURATION_RATIO
        )
        if duration > maximum_duration:
            raise ValueError(
                f"Slot {index} desired duration must not exceed "
                f"{maximum_duration:.3f} seconds for "
                f"target_clip_duration_sec={target_clip_duration_sec:.3f}; "
                "long dialogue must span multiple visual Slots as a start-aligned L-cut"
            )
        segment_id = str(item.get("source_segment_id") or "").strip()
        if not segment_id or segment_id not in segment_order:
            raise ValueError(
                f"Slot {index} must reference one valid source_segment_id"
            )
        # A failed retrieval does not prove that the whole source Segment is unusable.
        # A retry may keep the Segment while changing the requested evidence.
        raw_required_subjects = item.get("required_visible_subjects")
        if not isinstance(raw_required_subjects, list):
            raise ValueError(f"Slot {index} must list required_visible_subjects")
        required_subjects = [
            str(value).strip()
            for value in raw_required_subjects
            if str(value).strip()
        ]
        slots.append(
            {
                "slot_id": f"slot_{index:02d}",
                "narrative_role": str(item["narrative_role"]),
                "content_description": description,
                "target_emotion": str(item["target_emotion"]),
                "target_emotional_intensity": max(
                    0.0, min(1.0, float(item["target_emotional_intensity"]))
                ),
                "target_kinetic_energy": max(
                    0.0, min(1.0, float(item["target_kinetic_energy"]))
                ),
                "desired_duration_sec": duration,
                "continuity_from_previous": str(item["continuity_from_previous"]),
                "source_segment_id": segment_id,
                "required_visible_subjects": required_subjects,
            }
        )
    duration_total = sum(float(slot["desired_duration_sec"]) for slot in slots)
    if abs(duration_total - target_duration_sec) > MAX_DURATION_TOTAL_ERROR_SEC:
        raise ValueError(
            "Slot desired durations must total the requested output duration within "
            f"{MAX_DURATION_TOTAL_ERROR_SEC:.1f} seconds; got {duration_total:.3f} "
            f"for target {target_duration_sec:.3f}"
        )
    average_duration = duration_total / len(slots)
    maximum_average_error = _average_target_tolerance(
        target_duration_sec,
        target_clip_duration_sec,
    )
    if abs(average_duration - target_clip_duration_sec) > maximum_average_error:
        raise ValueError(
            "Average Slot duration must stay close to "
            f"target_clip_duration_sec={target_clip_duration_sec:.3f}; "
            f"got {average_duration:.3f} seconds across {len(slots)} Slots"
        )
    return _assign_source_groups(slots, video_description)


def plan_edit_slots(
    request: PlannersRequest,
    music_profile: dict[str, Any],
    config: LLMConfig,
    context: WorkflowContext,
    *,
    target_clip_duration_sec: float,
    output_fps: int,
) -> list[dict[str, Any]]:
    prime_arrangement_context(request, music_profile, context)
    return _plan_edit_slots_from_context(
        request,
        music_profile,
        config,
        context,
        target_clip_duration_sec=target_clip_duration_sec,
        output_fps=output_fps,
    )


def prime_arrangement_context(
    request: PlannersRequest,
    music_profile: dict[str, Any],
    context: WorkflowContext,
) -> None:
    """Rebuild request-derived in-memory context without a model call."""

    context.set_artifact("request", _request_metadata(request))
    context.set_artifact(
        "music_profile",
        compact_music_profile(music_profile),
    )
    video_description = context.get_artifact("video_description")
    if video_description is None:
        raise RuntimeError("Video description must be available before Slot arrangement")
    video_summary = context.get_artifact("video_summary")
    if video_summary is None:
        raise RuntimeError("Video summary must be available before Slot arrangement")
    context.set_artifact(
        "source_story_context",
        _source_story_context(video_description, video_summary),
    )
    return None


def _plan_edit_slots_from_context(
    request: PlannersRequest,
    music_profile: dict[str, Any],
    config: LLMConfig,
    context: WorkflowContext,
    *,
    target_clip_duration_sec: float,
    output_fps: int,
) -> list[dict[str, Any]]:
    video_description = context.get_artifact("video_description")
    if video_description is None:
        raise RuntimeError("Video description must be available before Slot arrangement")
    video_summary = context.get_artifact("video_summary")
    if video_summary is None:
        raise RuntimeError("Video summary must be available before Slot arrangement")
    planners_feedback = context.get_artifact("planners_feedback")
    retry_note = ""
    if planners_feedback:
        retry_note = (
            "\nThis is a redesign after an infeasible candidate path. Correct the failure using "
            "the maintained planners_feedback. A failed group may keep its source Segment "
            "when revised content and visible-subject requirements can produce a valid "
            "trajectory. Preserve group chronology.\n"
        )
    package = prompt_registry.build(
        PromptStage.PLANNERS,
        PromptTask.SLOT_ARRANGEMENT,
        SlotArrangementDetails(
            target_duration_sec=request.target_output_length_sec,
            target_clip_duration_sec=target_clip_duration_sec,
            allowed_segment_ids=[
                str(segment["segment_id"])
                for segment in video_description["segments"]
            ],
            retry_note=retry_note,
            mode="full",
            existing_slots=[],
            target_slot_constraints={},
            rejection_feedback=[],
        ),
    )
    slots = context.call_prompt(
        package=package,
        config=config,
        validate_business=lambda parsed: _validate_and_align_slots(
            parsed,
            request.target_output_length_sec,
            target_clip_duration_sec,
            video_description,
            music_profile,
            output_fps,
        ),
    )
    context.set_artifact("arrangement_groups", _arrangement_groups(slots))
    return slots


def _targeted_slot_constraints(
    slots: list[dict[str, Any]],
    target_slot_ids: set[str],
    video_description: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    source_segments = video_description["segments"]
    segment_ids = [str(segment["segment_id"]) for segment in source_segments]
    segment_order = {
        segment_id: index for index, segment_id in enumerate(segment_ids)
    }

    expanded_ids = _expand_target_group_slot_ids(slots, target_slot_ids)

    constraints: dict[str, dict[str, Any]] = {}
    for index, slot in enumerate(slots):
        slot_id = str(slot["slot_id"])
        if slot_id not in expanded_ids:
            continue
        previous_slot = next(
            (
                slots[position]
                for position in range(index - 1, -1, -1)
                if str(slots[position]["slot_id"]) not in expanded_ids
            ),
            None,
        )
        next_slot = next(
            (
                slots[position]
                for position in range(index + 1, len(slots))
                if str(slots[position]["slot_id"]) not in expanded_ids
            ),
            None,
        )
        lower = (
            segment_order[str(previous_slot["source_segment_id"])] + 1
            if previous_slot is not None
            else 0
        )
        upper = (
            segment_order[str(next_slot["source_segment_id"])] - 1
            if next_slot is not None
            else len(segment_ids) - 1
        )
        if lower > upper:
            raise ValueError(
                f"No chronological Segment interval remains for {slot_id}"
            )
        constraints[slot_id] = {
            "desired_duration_sec": float(slot["desired_duration_sec"]),
            "planned_duration_ms": int(slot["planned_duration_ms"]),
            "planned_duration_sec": float(slot["planned_duration_sec"]),
            "allowed_segment_ids": segment_ids[lower : upper + 1],
            "original_group_id": str(slot["group_id"]),
            "previous_fixed_slot": (
                {
                    "slot_id": previous_slot["slot_id"],
                    "source_segment_id": previous_slot["source_segment_id"],
                    "content_description": previous_slot["content_description"],
                }
                if previous_slot is not None
                else None
            ),
            "next_fixed_slot": (
                {
                    "slot_id": next_slot["slot_id"],
                    "source_segment_id": next_slot["source_segment_id"],
                    "content_description": next_slot["content_description"],
                }
                if next_slot is not None
                else None
            ),
        }
    return constraints


def _expand_target_group_slot_ids(
    slots: list[dict[str, Any]],
    target_slot_ids: set[str],
) -> set[str]:
    slot_to_group = {
        str(slot["slot_id"]): str(slot.get("group_id") or "")
        for slot in slots
    }
    unknown = target_slot_ids - set(slot_to_group)
    if unknown:
        raise ValueError(f"Unknown targeted Slot IDs: {sorted(unknown)}")
    target_group_ids = {slot_to_group[slot_id] for slot_id in target_slot_ids}
    if "" in target_group_ids:
        raise ValueError("Targeted Slot redesign requires canonical group_id values")
    return {
        str(slot["slot_id"])
        for slot in slots
        if str(slot.get("group_id") or "") in target_group_ids
    }


def _repair_window_slot_ids(
    slots: list[dict[str, Any]],
    target_slot_ids: set[str],
    video_description: dict[str, Any],
) -> set[str]:
    """Select the smallest independent repair window for each failed group run."""

    groups = _arrangement_groups(slots)
    group_index = {
        str(group["group_id"]): index for index, group in enumerate(groups)
    }
    slot_to_group = {
        str(slot["slot_id"]): str(slot["group_id"]) for slot in slots
    }
    expanded_target_ids = _expand_target_group_slot_ids(slots, target_slot_ids)
    requested_groups = {
        slot_to_group[slot_id] for slot_id in expanded_target_ids
    }
    requested_indices = sorted(group_index[group_id] for group_id in requested_groups)
    segment_order, segment_durations = _segment_metadata(video_description)
    segment_ids = [
        str(segment["segment_id"]) for segment in video_description["segments"]
    ]
    required_by_group = {
        str(group["group_id"]): sum(
            int(slot["planned_duration_ms"])
            for slot in slots
            if str(slot["group_id"]) == str(group["group_id"])
        )
        for group in groups
    }

    def allows_failed_groups_to_change(
        left: int,
        right: int,
        target_indices: tuple[int, ...],
    ) -> bool:
        lower = (
            segment_order[str(groups[left - 1]["source_segment_id"])] + 1
            if left > 0
            else 0
        )
        upper = (
            segment_order[str(groups[right + 1]["source_segment_id"])] - 1
            if right + 1 < len(groups)
            else len(segment_ids) - 1
        )
        if lower > upper:
            return False
        target_bits = {
            target_index: 1 << offset
            for offset, target_index in enumerate(target_indices)
        }
        all_targets_changed = (1 << len(target_bits)) - 1
        states: set[tuple[int, int]] = {(-1, 0)}
        for position in range(left, right + 1):
            group = groups[position]
            group_id = str(group["group_id"])
            required_ms = required_by_group[group_id]
            next_states: set[tuple[int, int]] = set()
            for candidate_index in range(lower, upper + 1):
                capacity_ms = segment_durations[segment_ids[candidate_index]]
                if required_ms > capacity_ms:
                    continue
                for previous_index, changed_targets in states:
                    if candidate_index <= previous_index:
                        continue
                    changed = changed_targets
                    if (
                        position in target_bits
                        and candidate_index
                        != segment_order[str(group["source_segment_id"])]
                    ):
                        changed |= target_bits[position]
                    next_states.add(
                        (
                            candidate_index,
                            changed,
                        )
                    )
            states = next_states
            if not states:
                return False
        return any(
            changed_targets == all_targets_changed
            for _, changed_targets in states
        )

    requested_runs: list[tuple[int, int]] = []
    for index in requested_indices:
        if requested_runs and index == requested_runs[-1][1] + 1:
            requested_runs[-1] = (requested_runs[-1][0], index)
        else:
            requested_runs.append((index, index))

    repair_windows: list[tuple[int, int, tuple[int, ...]]] = []
    for run_number, (initial_left, initial_right) in enumerate(requested_runs):
        minimum_left = (
            requested_runs[run_number - 1][1] + 1
            if run_number > 0
            else 0
        )
        maximum_right = (
            requested_runs[run_number + 1][0] - 1
            if run_number + 1 < len(requested_runs)
            else len(groups) - 1
        )
        maximum_expansion = max(
            initial_left - minimum_left,
            maximum_right - initial_right,
        )
        selected_window: tuple[int, int] | None = None
        for expansion in range(maximum_expansion + 1):
            left = max(minimum_left, initial_left - expansion)
            right = min(maximum_right, initial_right + expansion)
            if allows_failed_groups_to_change(
                left,
                right,
                tuple(range(initial_left, initial_right + 1)),
            ):
                selected_window = (left, right)
                break
        if selected_window is None:
            selected_window = (minimum_left, maximum_right)
        repair_windows.append(
            (
                selected_window[0],
                selected_window[1],
                tuple(range(initial_left, initial_right + 1)),
            )
        )

    def merge_repair_windows(
        windows: list[tuple[int, int, tuple[int, ...]]],
    ) -> list[tuple[int, int, tuple[int, ...]]]:
        merged: list[tuple[int, int, tuple[int, ...]]] = []
        for left, right, targets in sorted(windows):
            if not merged or left > merged[-1][1] + 1:
                merged.append((left, right, targets))
                continue
            previous_left, previous_right, previous_targets = merged[-1]
            merged[-1] = (
                previous_left,
                max(previous_right, right),
                tuple(sorted(set(previous_targets) | set(targets))),
            )
        return merged

    repair_windows = merge_repair_windows(repair_windows)
    while True:
        all_windows_feasible = True
        expanded_windows: list[tuple[int, int, tuple[int, ...]]] = []
        for left, right, targets in repair_windows:
            if allows_failed_groups_to_change(left, right, targets):
                expanded_windows.append((left, right, targets))
                continue
            expanded_left = max(0, left - 1)
            expanded_right = min(len(groups) - 1, right + 1)
            if expanded_left == left and expanded_right == right:
                raise ValueError(
                    "No Arrangement group repair window permits all failed groups "
                    "to change Segments in one assignment"
                )
            expanded_windows.append(
                (expanded_left, expanded_right, targets)
            )
            all_windows_feasible = False
        repair_windows = merge_repair_windows(expanded_windows)
        if all_windows_feasible:
            break

    selected_indices = {
        index
        for left, right, _targets in repair_windows
        for index in range(left, right + 1)
    }

    selected_group_ids = {
        str(groups[index]["group_id"]) for index in selected_indices
    }
    return {
        str(slot["slot_id"])
        for slot in slots
        if str(slot["group_id"]) in selected_group_ids
    }


def _validate_targeted_slots(
    parsed: dict[str, Any],
    slots: list[dict[str, Any]],
    constraints: dict[str, dict[str, Any]],
    video_description: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_slots = parsed.get("slots")
    if not isinstance(raw_slots, list):
        raise ValueError("Targeted Slot redesign must contain a slots array")
    expected_ids = set(constraints)
    replacements: dict[str, dict[str, Any]] = {}
    for item in raw_slots:
        if not isinstance(item, dict):
            raise ValueError("Each targeted Slot replacement must be an object")
        slot_id = str(item.get("slot_id") or "")
        if slot_id not in expected_ids or slot_id in replacements:
            raise ValueError(f"Unexpected targeted Slot ID: {slot_id}")
        constraint = constraints[slot_id]
        duration = float(item["desired_duration_sec"])
        if abs(duration - float(constraint["desired_duration_sec"])) > DURATION_TOLERANCE_SEC:
            raise ValueError(f"Targeted redesign changed desired duration for {slot_id}")
        planned_duration_ms = item.get("planned_duration_ms")
        if (
            not isinstance(planned_duration_ms, int)
            or isinstance(planned_duration_ms, bool)
            or planned_duration_ms != int(constraint["planned_duration_ms"])
        ):
            raise ValueError(f"Targeted redesign changed planned duration for {slot_id}")
        segment_id = str(item.get("source_segment_id") or "").strip()
        allowed = set(constraint["allowed_segment_ids"])
        if not segment_id or segment_id not in allowed:
            raise ValueError(
                f"Targeted redesign placed {slot_id} outside its chronological interval"
            )
        required_subjects = [
            str(value).strip()
            for value in item.get("required_visible_subjects") or []
            if str(value).strip()
        ]
        replacement = {
            "slot_id": slot_id,
            "narrative_role": str(item["narrative_role"]),
            "content_description": str(item["content_description"]).strip(),
            "target_emotion": str(item["target_emotion"]),
            "target_emotional_intensity": max(
                0.0, min(1.0, float(item["target_emotional_intensity"]))
            ),
            "target_kinetic_energy": max(
                0.0, min(1.0, float(item["target_kinetic_energy"]))
            ),
            "desired_duration_sec": duration,
            "planned_duration_ms": planned_duration_ms,
            "planned_duration_sec": planned_duration_ms / 1000.0,
            "continuity_from_previous": str(item["continuity_from_previous"]),
            "source_segment_id": segment_id,
            "required_visible_subjects": required_subjects,
        }
        if not replacement["content_description"]:
            raise ValueError(f"Targeted redesign left {slot_id} without visible content")
        replacements[slot_id] = replacement
    if set(replacements) != expected_ids:
        raise ValueError(
            f"Targeted redesign omitted Slots: {sorted(expected_ids - set(replacements))}"
        )

    merged: list[dict[str, Any]] = []
    for slot in slots:
        slot_id = str(slot["slot_id"])
        if slot_id not in replacements:
            merged.append(slot)
            continue
        updated = dict(slot)
        updated.update(replacements[slot_id])
        merged.append(updated)

    target_segment_by_original_group: dict[str, str] = {}
    for slot_id, replacement in replacements.items():
        original_group_id = str(constraints[slot_id]["original_group_id"])
        segment_id = str(replacement["source_segment_id"])
        previous = target_segment_by_original_group.setdefault(
            original_group_id,
            segment_id,
        )
        if previous != segment_id:
            raise ValueError(
                f"Targeted redesign split Source Group {original_group_id} "
                "across multiple Segments"
            )

    segment_order, _ = _segment_metadata(video_description)
    merged_by_slot_id = {str(slot["slot_id"]): slot for slot in merged}
    previous_segment_index = -1
    for original_group in _arrangement_groups(slots):
        group_segment_ids = {
            str(merged_by_slot_id[slot_id]["source_segment_id"])
            for slot_id in original_group["slot_ids"]
        }
        if len(group_segment_ids) != 1:
            raise ValueError(
                f"Targeted redesign split Source Group "
                f"{original_group['group_id']} across multiple Segments"
            )
        segment_id = next(iter(group_segment_ids))
        segment_index = segment_order[segment_id]
        if segment_index <= previous_segment_index:
            raise ValueError(
                "Arrangement group Segments must remain strictly increasing; "
                "distinct groups cannot merge onto one Segment"
            )
        previous_segment_index = segment_index

    grouped = _assign_source_groups(merged, video_description)
    _validate_group_capacity(grouped, video_description)
    return grouped


def redesign_edit_slots(
    slots: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    config: LLMConfig,
    context: WorkflowContext,
) -> tuple[list[dict[str, Any]], set[str]]:
    target_slot_ids = {
        str(failure["slot_id"]) for failure in failures
    }
    if not target_slot_ids:
        return slots, set()
    video_description = context.get_artifact("video_description")
    if video_description is None:
        raise RuntimeError("Video description must be available for targeted Slot redesign")
    original_target_slot_ids = _expand_target_group_slot_ids(
        slots,
        target_slot_ids,
    )
    expanded_slot_ids = _repair_window_slot_ids(
        slots,
        original_target_slot_ids,
        video_description,
    )
    constraints = _targeted_slot_constraints(
        slots,
        expanded_slot_ids,
        video_description,
    )
    if expanded_slot_ids != target_slot_ids:
        log_event(
            "WARNING",
            "aster.arrangement",
            "fallback.apply",
            "Expanded targeted Slot redesign to complete Source Groups",
            failed_slot_ids=sorted(target_slot_ids),
            replanned_slot_ids=sorted(expanded_slot_ids),
            allowed_segment_ids=sorted(
                {
                    segment_id
                    for constraint in constraints.values()
                    for segment_id in constraint["allowed_segment_ids"]
                }
            ),
        )
    package = prompt_registry.build(
        PromptStage.PLANNERS,
        PromptTask.SLOT_ARRANGEMENT,
        SlotArrangementDetails(
            target_duration_sec=(
                sum(int(slot["planned_duration_ms"]) for slot in slots) / 1000.0
            ),
            target_clip_duration_sec=(
                sum(int(slot["planned_duration_ms"]) for slot in slots)
                / 1000.0
                / len(slots)
            ),
            allowed_segment_ids=[],
            retry_note="",
            mode="targeted",
            existing_slots=slots,
            target_slot_constraints=constraints,
            rejection_feedback=failures,
        ),
    )
    redesigned = context.call_prompt(
        package=package,
        config=config,
        validate_business=lambda parsed: _validate_targeted_slots(
            parsed,
            slots,
            constraints,
            video_description,
        ),
    )
    context.set_artifact("edit_plan", redesigned)
    context.set_artifact("arrangement_groups", _arrangement_groups(redesigned))
    return redesigned, expanded_slot_ids


def align_slots_to_music(
    slots: list[dict[str, Any]],
    music_profile: dict[str, Any],
    total_duration_sec: float,
    output_fps: int,
    target_clip_duration_sec: float,
) -> list[dict[str, Any]]:
    weights = [max(0.1, float(slot["desired_duration_sec"])) for slot in slots]
    scale = total_duration_sec / sum(weights)
    elapsed = 0.0
    ideal: list[float] = []
    for weight in weights[:-1]:
        elapsed += weight * scale
        ideal.append(elapsed)
    accents = music_profile["accents_sec"]
    maximum_clip_duration_sec = (
        target_clip_duration_sec * MAX_TARGET_DURATION_RATIO
    )
    try:
        boundaries = _globally_align_boundaries(
            ideal,
            accents,
            total_duration_sec,
            output_fps,
            max_boundary_shift_sec=MAX_MUSIC_BOUNDARY_SHIFT_SEC,
            max_clip_duration_sec=maximum_clip_duration_sec,
        )
    except ValueError:
        try:
            boundaries = _globally_align_boundaries(
                ideal,
                music_profile["beats_sec"],
                total_duration_sec,
                output_fps,
                max_boundary_shift_sec=MAX_MUSIC_BOUNDARY_SHIFT_SEC,
                max_clip_duration_sec=maximum_clip_duration_sec,
            )
        except ValueError:
            boundaries = [
                round(value * output_fps) / output_fps
                for value in ideal
            ]
    aligned_durations = [
        end - start
        for start, end in zip(
            [0.0, *boundaries],
            [*boundaries, total_duration_sec],
            strict=True,
        )
    ]
    if any(
        duration > maximum_clip_duration_sec + 1.0 / output_fps
        for duration in aligned_durations
    ):
        raise ValueError(
            "Music alignment produced a visual Slot longer than "
            f"{maximum_clip_duration_sec:.3f} seconds"
        )
    edges_ms = [_milliseconds(value) for value in [0.0, *boundaries, total_duration_sec]]
    aligned: list[dict[str, Any]] = []
    for slot, start_ms, end_ms in zip(
        slots,
        edges_ms[:-1],
        edges_ms[1:],
        strict=True,
    ):
        planned_duration_ms = end_ms - start_ms
        if planned_duration_ms <= 0:
            raise ValueError("Music alignment produced a non-positive Slot duration")
        item = dict(slot)
        item["output_start_sec"] = start_ms / 1000.0
        item["output_end_sec"] = end_ms / 1000.0
        item["planned_duration_ms"] = planned_duration_ms
        item["planned_duration_sec"] = planned_duration_ms / 1000.0
        aligned.append(item)
    return aligned


def _validate_and_align_slots(
    parsed: dict[str, Any],
    target_duration_sec: float,
    target_clip_duration_sec: float,
    video_description: dict[str, Any],
    music_profile: dict[str, Any],
    output_fps: int,
) -> list[dict[str, Any]]:
    slots = _validate_slots(
        parsed,
        target_duration_sec,
        target_clip_duration_sec,
        video_description,
    )
    aligned = align_slots_to_music(
        slots,
        music_profile,
        target_duration_sec,
        output_fps,
        target_clip_duration_sec,
    )
    _validate_group_capacity(aligned, video_description)
    return aligned


def _globally_align_boundaries(
    ideal: list[float],
    candidates: list[float],
    total_duration_sec: float,
    output_fps: int,
    min_clip_duration_sec: float = 1.5,
    max_boundary_shift_sec: float | None = None,
    max_clip_duration_sec: float | None = None,
) -> list[float]:
    if not ideal:
        return []
    values = sorted(
        {
            round(float(value) * output_fps) / output_fps
            for value in candidates
            if min_clip_duration_sec
            <= float(value)
            <= total_duration_sec - min_clip_duration_sec
        }
    )
    if len(values) < len(ideal):
        raise ValueError("Not enough musical accents to align all edit boundaries")
    states: dict[int, tuple[float, list[float]]] = {}
    for index, value in enumerate(values):
        if (
            value >= min_clip_duration_sec
            and (
                max_clip_duration_sec is None
                or value <= max_clip_duration_sec
            )
            and (
                max_boundary_shift_sec is None
                or abs(value - ideal[0]) <= max_boundary_shift_sec
            )
        ):
            states[index] = (abs(value - ideal[0]), [value])
    for boundary_index in range(1, len(ideal)):
        next_states: dict[int, tuple[float, list[float]]] = {}
        for index, value in enumerate(values):
            if (
                max_boundary_shift_sec is not None
                and abs(value - ideal[boundary_index]) > max_boundary_shift_sec
            ):
                continue
            best: tuple[float, list[float]] | None = None
            for previous_index, (cost, path) in states.items():
                clip_duration = value - path[-1]
                if (
                    previous_index >= index
                    or clip_duration < min_clip_duration_sec
                    or (
                        max_clip_duration_sec is not None
                        and clip_duration > max_clip_duration_sec
                    )
                ):
                    continue
                proposal = (cost + abs(value - ideal[boundary_index]), [*path, value])
                if best is None or proposal[0] < best[0]:
                    best = proposal
            if best is not None:
                next_states[index] = best
        states = next_states
        if not states:
            raise ValueError("No monotonic musical-boundary path satisfies minimum clip length")
    feasible = [
        state
        for state in states.values()
        if total_duration_sec - state[1][-1] >= min_clip_duration_sec
        and (
            max_clip_duration_sec is None
            or total_duration_sec - state[1][-1] <= max_clip_duration_sec
        )
    ]
    if not feasible:
        raise ValueError("No musical-boundary path leaves room for the final clip")
    return min(feasible, key=lambda state: state[0])[1]


class ArrangementArchitectAgent:
    """A agent: arrange Slots, pacing, emotion, and narrative structure."""

    def __init__(self, config: AppConfig, context: WorkflowContext) -> None:
        self.config = config
        self.context = context

    def profile_music(
        self,
        music_memory: dict[str, Any],
        target_duration_sec: float,
        output_path: Path,
    ) -> dict[str, Any]:
        profile = build_music_profile(music_memory, target_duration_sec)
        write_music_profile(output_path, profile)
        self.context.set_artifact(
            "music_profile",
            compact_music_profile(profile),
        )
        return profile

    def arrange(
        self,
        request: PlannersRequest,
        music_profile: dict[str, Any],
    ) -> list[dict[str, Any]]:
        model_config = replace(
            self.config.llm,
            max_retries=(
                self.config.planners.arrangement_architect.max_model_requests - 1
            ),
        )
        return plan_edit_slots(
            request,
            music_profile,
            model_config,
            self.context,
            target_clip_duration_sec=(
                self.config.planners.arrangement_architect.target_clip_duration_sec
            ),
            output_fps=self.config.renderer.fps,
        )

    def repair(
        self,
        slots: list[dict[str, Any]],
        failures: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], set[str]]:
        model_config = replace(
            self.config.llm,
            max_retries=(
                self.config.planners.arrangement_architect.max_model_requests - 1
            ),
        )
        return redesign_edit_slots(
            slots,
            failures,
            model_config,
            self.context,
        )


__all__ = ["ArrangementArchitectAgent"]
