from __future__ import annotations

from typing import Any

from cutmaster.configuration.schema import LLMConfig
from cutmaster.contracts.workflow import RunRequest
from cutmaster.music.analysis import compact_music_profile
from cutmaster.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.prompting.planner import SlotPlanningDetails
from cutmaster.runtime.workflow_context import WorkflowContext

MIN_SLOT_DURATION_SEC = 1.5
MAX_DURATION_TOTAL_ERROR_SEC = 0.5
MAX_MUSIC_BOUNDARY_SHIFT_SEC = 0.75
MAX_TARGET_DURATION_RATIO = 1.5
MAX_AVERAGE_TARGET_ERROR_RATIO = 0.125
DURATION_TOLERANCE_SEC = 1e-6


def _request_metadata(request: RunRequest) -> dict[str, Any]:
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
    forbidden_segment_ids: set[str],
    failed_segment_assignments: set[tuple[str, ...]],
) -> list[dict[str, Any]]:
    raw = parsed.get("slots")
    if not isinstance(raw, list) or not raw:
        raise ValueError("Expected at least one edit slot")
    slots: list[dict[str, Any]] = []
    source_segments = video_description["segments"]
    segment_order = {
        str(segment["segment_id"]): index
        for index, segment in enumerate(source_segments)
    }
    previous_segment_end_index = -1
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
        segment_ids = [
            str(value).strip()
            for value in item.get("source_segment_ids") or []
            if str(value).strip()
        ]
        if not segment_ids or any(
            segment_id not in segment_order for segment_id in segment_ids
        ):
            raise ValueError(f"Slot {index} must reference valid source Segment IDs")
        if any(segment_id in forbidden_segment_ids for segment_id in segment_ids):
            raise ValueError(f"Slot {index} reuses a visually disproven source Segment")
        if tuple(segment_ids) in failed_segment_assignments:
            raise ValueError(f"Slot {index} repeats a failed source-Segment assignment")
        segment_start_index = min(
            segment_order[segment_id] for segment_id in segment_ids
        )
        segment_end_index = max(
            segment_order[segment_id] for segment_id in segment_ids
        )
        if segment_start_index <= previous_segment_end_index:
            raise ValueError(
                "Slot source Segment ranges must be in strictly increasing source order"
            )
        previous_segment_end_index = segment_end_index
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
                "source_segment_ids": segment_ids,
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
    return slots


def plan_edit_slots(
    request: RunRequest,
    music_profile: dict[str, Any],
    config: LLMConfig,
    context: WorkflowContext,
    *,
    target_clip_duration_sec: float,
) -> list[dict[str, Any]]:
    context.set_artifact("request", _request_metadata(request))
    context.set_artifact(
        "music_profile",
        compact_music_profile(music_profile),
    )
    video_description = context.get_artifact("video_description")
    if video_description is None:
        raise RuntimeError("Video description must be available before edit-slot planning")
    video_summary = context.get_artifact("video_summary")
    if video_summary is None:
        raise RuntimeError("Video summary must be available before edit-slot planning")
    context.set_artifact(
        "source_story_context",
        _source_story_context(video_description, video_summary),
    )
    planning_feedback = context.get_artifact("planning_feedback")
    forbidden_segment_ids = {
        str(value)
        for value in (planning_feedback or {}).get("forbidden_segment_ids") or []
    }
    failed_segment_assignments = {
        tuple(str(value) for value in failed.get("source_segment_ids") or [])
        for failed in (planning_feedback or {}).get("failed_slots") or []
        if failed.get("source_segment_ids")
    }
    retry_note = ""
    if planning_feedback:
        retry_note = (
            "\nThis is a replan after an infeasible candidate path. Correct the failure using "
            "the maintained planning_feedback. Never use a Segment in forbidden_segment_ids. "
            "Do not repeat any exact source_segment_ids assignment listed in failed_slots; "
            "choose different visual source evidence while preserving chronology.\n"
        )
    package = prompt_registry.build(
        PromptStage.PLANNER,
        PromptTask.SLOT_PLANNING,
        SlotPlanningDetails(
            target_duration_sec=request.target_output_length_sec,
            target_clip_duration_sec=target_clip_duration_sec,
            allowed_segment_ids=[
                str(segment["segment_id"])
                for segment in video_description["segments"]
                if str(segment["segment_id"]) not in forbidden_segment_ids
            ],
            retry_note=retry_note,
            mode="full",
            existing_slots=[],
            target_slot_constraints={},
            rejection_feedback=[],
        ),
    )
    return context.call_prompt(
        package=package,
        config=config,
        validate_business=lambda parsed: _validate_slots(
            parsed,
            request.target_output_length_sec,
            target_clip_duration_sec,
            video_description,
            forbidden_segment_ids,
            failed_segment_assignments,
        ),
    )


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

    def start_index(slot: dict[str, Any]) -> int:
        return min(
            segment_order[str(segment_id)]
            for segment_id in slot["source_segment_ids"]
        )

    def end_index(slot: dict[str, Any]) -> int:
        return max(
            segment_order[str(segment_id)]
            for segment_id in slot["source_segment_ids"]
        )

    constraints: dict[str, dict[str, Any]] = {}
    for index, slot in enumerate(slots):
        slot_id = str(slot["slot_id"])
        if slot_id not in target_slot_ids:
            continue
        previous_slot = next(
            (
                slots[position]
                for position in range(index - 1, -1, -1)
                if str(slots[position]["slot_id"]) not in target_slot_ids
            ),
            None,
        )
        next_slot = next(
            (
                slots[position]
                for position in range(index + 1, len(slots))
                if str(slots[position]["slot_id"]) not in target_slot_ids
            ),
            None,
        )
        lower = end_index(previous_slot) + 1 if previous_slot is not None else 0
        upper = (
            start_index(next_slot) - 1
            if next_slot is not None
            else len(segment_ids) - 1
        )
        if lower > upper:
            raise ValueError(
                f"No chronological Segment interval remains for {slot_id}"
            )
        constraints[slot_id] = {
            "desired_duration_sec": float(slot["desired_duration_sec"]),
            "planned_duration_sec": float(slot["planned_duration_sec"]),
            "allowed_segment_ids": segment_ids[lower : upper + 1],
            "previous_fixed_slot": (
                {
                    "slot_id": previous_slot["slot_id"],
                    "source_segment_ids": previous_slot["source_segment_ids"],
                    "content_description": previous_slot["content_description"],
                }
                if previous_slot is not None
                else None
            ),
            "next_fixed_slot": (
                {
                    "slot_id": next_slot["slot_id"],
                    "source_segment_ids": next_slot["source_segment_ids"],
                    "content_description": next_slot["content_description"],
                }
                if next_slot is not None
                else None
            ),
            "previous_source_segment_ids": list(slot["source_segment_ids"]),
            "forbidden_segment_assignments": [],
        }
    return constraints


def _validate_targeted_slots(
    parsed: dict[str, Any],
    slots: list[dict[str, Any]],
    constraints: dict[str, dict[str, Any]],
    video_description: dict[str, Any],
) -> list[dict[str, Any]]:
    raw_slots = parsed.get("slots")
    if not isinstance(raw_slots, list):
        raise ValueError("Targeted Slot replan must contain a slots array")
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
            raise ValueError(f"Targeted replan changed desired duration for {slot_id}")
        planned_duration = float(item["planned_duration_sec"])
        if (
            abs(
                planned_duration
                - float(constraint["planned_duration_sec"])
            )
            > DURATION_TOLERANCE_SEC
        ):
            raise ValueError(f"Targeted replan changed planned duration for {slot_id}")
        segment_ids = [
            str(value).strip()
            for value in item.get("source_segment_ids") or []
            if str(value).strip()
        ]
        allowed = set(constraint["allowed_segment_ids"])
        if not segment_ids or any(segment_id not in allowed for segment_id in segment_ids):
            raise ValueError(
                f"Targeted replan placed {slot_id} outside its chronological interval"
            )
        if segment_ids in constraint["forbidden_segment_assignments"]:
            raise ValueError(
                f"Targeted replan repeated a forbidden Segment assignment for {slot_id}"
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
            "planned_duration_sec": planned_duration,
            "continuity_from_previous": str(item["continuity_from_previous"]),
            "source_segment_ids": segment_ids,
            "required_visible_subjects": required_subjects,
        }
        if not replacement["content_description"]:
            raise ValueError(f"Targeted replan left {slot_id} without visible content")
        replacements[slot_id] = replacement
    if set(replacements) != expected_ids:
        raise ValueError(
            f"Targeted replan omitted Slots: {sorted(expected_ids - set(replacements))}"
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

    segment_order = {
        str(segment["segment_id"]): index
        for index, segment in enumerate(video_description["segments"])
    }
    previous_end_index = -1
    for slot in merged:
        current_start_index = min(
            segment_order[str(segment_id)]
            for segment_id in slot["source_segment_ids"]
        )
        current_end_index = max(
            segment_order[str(segment_id)]
            for segment_id in slot["source_segment_ids"]
        )
        if current_start_index <= previous_end_index:
            raise ValueError(
                "Targeted Slot replacements violate strictly increasing source order"
            )
        previous_end_index = current_end_index
    return merged


def redesign_edit_slots(
    slots: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    config: LLMConfig,
    context: WorkflowContext,
) -> list[dict[str, Any]]:
    target_slot_ids = {
        str(failure["slot_id"]) for failure in failures
    }
    if not target_slot_ids:
        return slots
    video_description = context.get_artifact("video_description")
    if video_description is None:
        raise RuntimeError("Video description must be available for targeted Slot replanning")
    constraints = _targeted_slot_constraints(
        slots,
        target_slot_ids,
        video_description,
    )
    package = prompt_registry.build(
        PromptStage.PLANNER,
        PromptTask.SLOT_PLANNING,
        SlotPlanningDetails(
            target_duration_sec=sum(
                float(slot["planned_duration_sec"]) for slot in slots
            ),
            target_clip_duration_sec=(
                sum(float(slot["planned_duration_sec"]) for slot in slots)
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
    return redesigned


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
    edges = [0.0, *boundaries, total_duration_sec]
    aligned: list[dict[str, Any]] = []
    for slot, start, end in zip(slots, edges[:-1], edges[1:], strict=True):
        item = dict(slot)
        item["output_start_sec"] = round(start, 6)
        item["output_end_sec"] = round(end, 6)
        item["planned_duration_sec"] = round(end - start, 6)
        aligned.append(item)
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
