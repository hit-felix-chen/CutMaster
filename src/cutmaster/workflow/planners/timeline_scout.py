from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import cv2

from cutmaster.configuration.schema import (
    AppConfig,
    CandidateRetrievalConfig,
    LLMConfig,
    VLMConfig,
)
from cutmaster.infrastructure.observability.logging import error_summary, log_event
from cutmaster.infrastructure.observability.progress import progress_bar
from cutmaster.workflow.planners.edit_composer import (
    _globally_viable_trajectory_ids,
    _trajectory_units,
    score_unary_candidate,
)
from cutmaster.workflow.planners.tools.errors import GroupNoCandidateError
from cutmaster.workflow.planners.tools.segment_media import SegmentMediaReader
from cutmaster.workflow.planners.tools.visual_scoring import _contact_sheet_data_url
from cutmaster.workflow.ports import CancellationToken, raise_if_cancelled
from cutmaster.workflow.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.workflow.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)
from cutmaster.workflow.prompting.planners import (
    CandidateRetrievalDetails,
    CandidateVisualScoringDetails,
    candidate_trajectory_contract,
)
from cutmaster.workflow.shared.execution_context import WorkflowContext
from cutmaster.workflow.shared.timecode import format_range, parse_range


def _seconds_to_ms(value: Any) -> int:
    return int(round(float(value) * 1000.0))


def _bounded_score(value: Any, field: str) -> float:
    # The shared trajectory schema already validates numeric types and bounds.
    # Reject non-standard JSON NaN values that schema range comparisons miss.
    score = float(value)
    if not math.isfinite(score):
        raise ValueError(f"{field} must be finite")
    return score


def _range_ms(timestamp: str) -> tuple[int, int]:
    start_sec, end_sec = parse_range(timestamp)
    return _seconds_to_ms(start_sec), _seconds_to_ms(end_sec)


def _ranges_overlap(
    first: tuple[int, int],
    second: tuple[int, int],
) -> bool:
    return first[0] < second[1] and second[0] < first[1]


def _normalize_candidate_ranges(
    proposed_starts: list[int],
    durations: list[int],
    *,
    planning_start_ms: int,
    planning_end_ms: int,
) -> list[tuple[int, int]]:
    if len(proposed_starts) != len(durations):
        raise ValueError("Candidate starts and Slot durations do not align")
    if sum(durations) > planning_end_ms - planning_start_ms:
        raise ValueError("Slot Group does not fit inside its Planning Segment")

    ranges: list[list[int]] = []
    previous_end_ms = planning_start_ms
    for proposed_start_ms, duration_ms in zip(
        proposed_starts,
        durations,
        strict=True,
    ):
        latest_start_ms = planning_end_ms - duration_ms
        start_ms = min(
            max(proposed_start_ms, planning_start_ms),
            latest_start_ms,
        )
        start_ms = max(start_ms, previous_end_ms)
        end_ms = start_ms + duration_ms
        ranges.append([start_ms, end_ms])
        previous_end_ms = end_ms

    if ranges and ranges[-1][1] > planning_end_ms:
        next_start_ms = planning_end_ms
        for index in range(len(ranges) - 1, -1, -1):
            duration_ms = durations[index]
            start_ms = min(ranges[index][0], next_start_ms - duration_ms)
            ranges[index] = [start_ms, start_ms + duration_ms]
            next_start_ms = start_ms

    normalized = [(start_ms, end_ms) for start_ms, end_ms in ranges]
    previous_end_ms = planning_start_ms
    for start_ms, end_ms in normalized:
        if (
            start_ms < planning_start_ms
            or end_ms > planning_end_ms
            or start_ms < previous_end_ms
        ):
            raise ValueError(
                "Could not place Slot Group inside its Planning Segment"
            )
        previous_end_ms = end_ms
    return normalized


def _trajectory_batch_size(
    slots: list[dict[str, Any]],
    planning_segment: dict[str, Any],
    target: int,
) -> int:
    durations = [int(slot["planned_duration_ms"]) for slot in slots]
    segment_duration_ms = int(planning_segment["end_ms"]) - int(
        planning_segment["start_ms"]
    )
    slack_ms = max(0, segment_duration_ms - sum(durations))
    distinct_window_stride_ms = max(durations)
    conservative_layout_count = 1 + slack_ms // distinct_window_stride_ms
    return max(1, min(target, conservative_layout_count))


def _candidate_overlap_shot_ids(candidate: dict[str, Any]) -> set[str]:
    raw_shot_ids = candidate.get("source_shot_ids")
    if not isinstance(raw_shot_ids, list):
        return set()
    return {
        str(shot_id).strip()
        for shot_id in raw_shot_ids
        if str(shot_id).strip()
    }


def _candidate_duplicates_source_evidence(
    candidate: dict[str, Any],
    existing: dict[str, Any],
) -> bool:
    candidate_shot_ids = _candidate_overlap_shot_ids(candidate)
    existing_shot_ids = _candidate_overlap_shot_ids(existing)
    if (
        candidate_shot_ids
        and existing_shot_ids
        and not candidate_shot_ids.intersection(existing_shot_ids)
    ):
        return False
    return _ranges_overlap(
        _range_ms(str(candidate["timestamp"])),
        _range_ms(str(existing["timestamp"])),
    )


def _source_shot_ids_for_range(
    source_segment: dict[str, Any],
    candidate_range: tuple[int, int],
) -> list[str]:
    return [
        str(shot["shot_id"])
        for shot in source_segment["shots"]
        if _ranges_overlap(
            candidate_range,
            (
                _seconds_to_ms(shot["time_range"]["start_sec"]),
                _seconds_to_ms(shot["time_range"]["end_sec"]),
            ),
        )
    ]


def _viable_trajectory_counts(
    slots: list[dict[str, Any]],
    pool: dict[str, list[dict[str, Any]]],
) -> dict[str, int]:
    local_counts = {
        group_id: len(trajectories)
        for group_id, trajectories in pool.items()
    }
    if not local_counts:
        return {}
    if any(count == 0 for count in local_counts.values()):
        return local_counts
    units = _trajectory_units(slots, pool)
    viable_ids = _globally_viable_trajectory_ids(units)
    return {
        str(unit["group_id"]): len(ids)
        for unit, ids in zip(units, viable_ids, strict=True)
        if not bool(unit["trajectories"][0].get("fixed"))
    }


def _planning_units(
    slots: list[dict[str, Any]],
    context: WorkflowContext,
) -> list[dict[str, Any]]:
    """Validate the Story output and return non-Anchor groups in Slot order."""

    raw_groups = context.get_artifact("planning_groups")
    raw_segments = context.get_artifact("planning_segments")
    if not isinstance(raw_groups, list) or not isinstance(raw_segments, list):
        raise RuntimeError(
            "Story Editor must publish planning_groups and planning_segments"
        )
    segments: dict[str, dict[str, Any]] = {}
    for raw in raw_segments:
        if not isinstance(raw, dict):
            raise ValueError("Planning Segment must be an object")
        planning_segment_id = str(raw.get("planning_segment_id") or "")
        source_segment_id = str(raw.get("source_segment_id") or "")
        start_ms = raw.get("start_ms")
        end_ms = raw.get("end_ms")
        if (
            not planning_segment_id
            or planning_segment_id in segments
            or not source_segment_id
            or isinstance(start_ms, bool)
            or not isinstance(start_ms, int)
            or isinstance(end_ms, bool)
            or not isinstance(end_ms, int)
            or start_ms < 0
            or end_ms <= start_ms
        ):
            raise ValueError("Invalid or duplicate Planning Segment")
        segments[planning_segment_id] = {
            "planning_segment_id": planning_segment_id,
            "source_segment_id": source_segment_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
        }

    all_slot_ids = [str(slot["slot_id"]) for slot in slots]
    if len(set(all_slot_ids)) != len(all_slot_ids):
        raise ValueError("Slot IDs must be unique before trajectory retrieval")
    slots_by_id = dict(zip(all_slot_ids, slots, strict=True))
    slot_positions = {
        slot_id: position for position, slot_id in enumerate(all_slot_ids)
    }
    ordinary_slot_ids = [
        str(slot["slot_id"])
        for slot in slots
        if slot.get("fixed_candidate") is None
    ]
    used_slot_ids: list[str] = []
    units: list[dict[str, Any]] = []
    group_ids: set[str] = set()
    used_planning_segment_ids: set[str] = set()
    for raw in raw_groups:
        if not isinstance(raw, dict):
            raise ValueError("Planning Group must be an object")
        group_id = str(raw.get("group_id") or "")
        parent_group_id = str(raw.get("parent_group_id") or "")
        source_segment_id = str(raw.get("source_segment_id") or "")
        planning_segment_id = str(raw.get("planning_segment_id") or "")
        slot_ids = raw.get("slot_ids")
        if (
            not group_id
            or group_id in group_ids
            or not parent_group_id
            or not source_segment_id
            or not isinstance(slot_ids, list)
            or not slot_ids
        ):
            raise ValueError("Invalid or duplicate Planning Group")
        group_ids.add(group_id)
        normalized_slot_ids = [str(value) for value in slot_ids]
        if len(set(normalized_slot_ids)) != len(normalized_slot_ids):
            raise ValueError(f"Planning Group {group_id} repeats a Slot ID")
        if any(slot_id not in slots_by_id for slot_id in normalized_slot_ids):
            raise ValueError(f"Planning Group {group_id} references unknown Slots")
        positions = [slot_positions[slot_id] for slot_id in normalized_slot_ids]
        if positions != list(range(positions[0], positions[0] + len(positions))):
            raise ValueError(f"Planning Group {group_id} crosses a Slot boundary")
        group_slots = [slots_by_id[slot_id] for slot_id in normalized_slot_ids]
        if any(slot.get("fixed_candidate") is not None for slot in group_slots):
            raise ValueError(f"Planning Group {group_id} contains an Anchor Slot")
        if any(str(slot.get("group_id") or "") != group_id for slot in group_slots):
            raise ValueError(f"Planning Group {group_id} disagrees with its Slots")
        if any(
            str(slot.get("parent_group_id") or "") != parent_group_id
            for slot in group_slots
        ):
            raise ValueError(f"Planning Group {group_id} has inconsistent parent")
        if any(
            str(slot.get("source_segment_id") or "") != source_segment_id
            or str(slot.get("planning_segment_id") or "") != planning_segment_id
            for slot in group_slots
        ):
            raise ValueError(f"Planning Group {group_id} has inconsistent Segment binding")
        if any(
            isinstance(slot.get("planned_duration_ms"), bool)
            or not isinstance(slot.get("planned_duration_ms"), int)
            or int(slot["planned_duration_ms"]) <= 0
            for slot in group_slots
        ):
            raise ValueError(
                f"Planning Group {group_id} has invalid planned_duration_ms"
            )
        segment = segments.get(planning_segment_id)
        if segment is None or segment["source_segment_id"] != source_segment_id:
            raise ValueError(f"Planning Group {group_id} has no matching Planning Segment")
        if planning_segment_id in used_planning_segment_ids:
            raise ValueError(
                f"Planning Segment {planning_segment_id} is bound to multiple groups"
            )
        used_planning_segment_ids.add(planning_segment_id)
        first_position = positions[0]
        last_position = positions[-1]
        if first_position > 0:
            previous_slot = slots[first_position - 1]
            previous_anchor = previous_slot.get("fixed_candidate")
            if (
                previous_anchor is not None
                and str(previous_slot.get("parent_group_id") or "")
                == parent_group_id
                and str(previous_slot.get("source_segment_id") or "")
                == source_segment_id
            ):
                _anchor_start_ms, anchor_end_ms = _range_ms(
                    str(previous_anchor.get("timestamp") or "")
                )
                if int(segment["start_ms"]) < anchor_end_ms:
                    raise ValueError(
                        f"Planning Group {group_id} crosses its previous Anchor"
                    )
        if last_position + 1 < len(slots):
            next_slot = slots[last_position + 1]
            next_anchor = next_slot.get("fixed_candidate")
            if (
                next_anchor is not None
                and str(next_slot.get("parent_group_id") or "")
                == parent_group_id
                and str(next_slot.get("source_segment_id") or "")
                == source_segment_id
            ):
                anchor_start_ms, _anchor_end_ms = _range_ms(
                    str(next_anchor.get("timestamp") or "")
                )
                if int(segment["end_ms"]) > anchor_start_ms:
                    raise ValueError(
                        f"Planning Group {group_id} crosses its next Anchor"
                    )
        required_ms = sum(slot["planned_duration_ms"] for slot in group_slots)
        if required_ms > int(segment["end_ms"]) - int(segment["start_ms"]):
            raise ValueError(f"Planning Group {group_id} exceeds its Planning Segment")
        used_slot_ids.extend(normalized_slot_ids)
        units.append(
            {
                "group": {
                    "group_id": group_id,
                    "parent_group_id": parent_group_id,
                    "source_segment_id": source_segment_id,
                    "planning_segment_id": planning_segment_id,
                    "slot_ids": normalized_slot_ids,
                },
                "slots": group_slots,
                "planning_segment": segment,
            }
        )
    if used_slot_ids != ordinary_slot_ids:
        raise ValueError(
            "Planning Groups must cover every non-Anchor Slot exactly once and in order"
        )
    if used_planning_segment_ids != set(segments):
        raise ValueError("Planning Segments must map one-to-one to Planning Groups")
    video_description = context.get_artifact("video_description")
    if not isinstance(video_description, dict):
        raise RuntimeError("Video description is required for trajectory retrieval")
    source_positions = {
        str(segment["segment_id"]): position
        for position, segment in enumerate(video_description.get("segments") or [])
    }
    anchor_group_ids: set[str] = set()
    previous_anchor_end_ms = -1
    for slot in slots:
        candidate = slot.get("fixed_candidate")
        if candidate is None:
            continue
        if not isinstance(candidate, dict):
            raise ValueError("Anchor fixed_candidate must be an object")
        slot_id = str(slot.get("slot_id") or "")
        group_id = str(slot.get("group_id") or "")
        parent_group_id = str(slot.get("parent_group_id") or "")
        source_segment_id = str(slot.get("source_segment_id") or "")
        if (
            not group_id
            or group_id in group_ids
            or group_id in anchor_group_ids
            or not parent_group_id
        ):
            raise ValueError(f"Anchor Slot {slot_id} has invalid group identity")
        anchor_group_ids.add(group_id)
        if (
            str(candidate.get("slot_id") or "") != slot_id
            or str(candidate.get("source_segment_id") or "") != source_segment_id
        ):
            raise ValueError(f"Anchor Slot {slot_id} has inconsistent source binding")
        dialogue_anchor = slot.get("dialogue_anchor")
        if (
            not isinstance(dialogue_anchor, dict)
            or str(dialogue_anchor.get("source_segment_id") or "")
            != source_segment_id
            or str(dialogue_anchor.get("source_video_timestamp") or "")
            != str(candidate.get("timestamp") or "")
        ):
            raise ValueError(f"Anchor Slot {slot_id} has inconsistent dialogue binding")
        start_ms, end_ms = _range_ms(str(candidate.get("timestamp") or ""))
        planned_duration_ms = slot.get("planned_duration_ms")
        if (
            isinstance(planned_duration_ms, bool)
            or not isinstance(planned_duration_ms, int)
            or planned_duration_ms <= 0
            or end_ms - start_ms != planned_duration_ms
        ):
            raise ValueError(f"Anchor Slot {slot_id} has inconsistent duration")
        _source_segment_context(
            {
                "source_segment_id": source_segment_id,
                "start_ms": start_ms,
                "end_ms": end_ms,
            },
            video_description,
        )
        if start_ms < previous_anchor_end_ms:
            raise ValueError("Anchor picture windows must follow source order")
        previous_anchor_end_ms = end_ms
    for previous_slot, current_slot in zip(slots, slots[1:]):
        previous_source = str(previous_slot.get("source_segment_id") or "")
        current_source = str(current_slot.get("source_segment_id") or "")
        if (
            previous_source not in source_positions
            or current_source not in source_positions
        ):
            raise ValueError("Slot Group references an unknown source Segment")
        if source_positions[current_source] < source_positions[previous_source]:
            raise ValueError("Slot source Segments must be monotonically nondecreasing")
        if (
            previous_slot.get("fixed_candidate") is not None
            or current_slot.get("fixed_candidate") is not None
        ):
            continue
        same_group = str(previous_slot.get("group_id")) == str(
            current_slot.get("group_id")
        )
        if same_group != (previous_source == current_source):
            raise ValueError(
                "Adjacent ordinary Slots share a source Segment exactly when "
                "they share a Planning Group"
            )
    return units


def _source_segment_context(
    planning_segment: dict[str, Any],
    video_description: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    source_segment_id = str(planning_segment["source_segment_id"])
    source_segment = next(
        (
            segment
            for segment in video_description["segments"]
            if str(segment["segment_id"]) == source_segment_id
        ),
        None,
    )
    if source_segment is None:
        raise ValueError(f"Unknown source Segment: {source_segment_id}")
    source_start_ms = _seconds_to_ms(source_segment["time_range"]["start_sec"])
    source_end_ms = _seconds_to_ms(source_segment["time_range"]["end_sec"])
    start_ms = int(planning_segment["start_ms"])
    end_ms = int(planning_segment["end_ms"])
    if start_ms < source_start_ms or end_ms > source_end_ms:
        raise ValueError("Planning Segment exceeds its source Segment")
    planning_range = (start_ms, end_ms)
    compact = {
        key: value
        for key, value in source_segment.items()
        if key not in {"shots", "time_range"}
    }
    compact.update(
        {
            **planning_segment,
            "time_range": {
                "start_sec": start_ms / 1000.0,
                "end_sec": end_ms / 1000.0,
            },
            "shots": [
                {
                    key: value
                    for key, value in shot.items()
                    if key not in {"dialogue", "sampled_frame_times_sec"}
                }
                for shot in source_segment["shots"]
                if _ranges_overlap(
                    planning_range,
                    (
                        _seconds_to_ms(shot["time_range"]["start_sec"]),
                        _seconds_to_ms(shot["time_range"]["end_sec"]),
                    ),
                )
            ],
        }
    )
    return compact, source_segment


def _validate_trajectory_response(
    parsed: Any,
    *,
    group: dict[str, Any],
    slots: list[dict[str, Any]],
    planning_segment: dict[str, Any],
    source_segment: dict[str, Any],
    requested_count: int,
) -> dict[str, list[dict[str, Any]]]:
    if not isinstance(parsed, dict):
        raise ValueError("Trajectory response must be an object")
    raw_trajectories = parsed.get("trajectories")
    if (
        not isinstance(raw_trajectories, list)
        or len(raw_trajectories) > requested_count
    ):
        raise ValueError(f"Expected at most {requested_count} complete trajectories")
    group_id = str(group["group_id"])
    planning_segment_id = str(group["planning_segment_id"])
    source_segment_id = str(group["source_segment_id"])
    expected_slot_ids = [str(slot["slot_id"]) for slot in slots]
    planning_start_ms = int(planning_segment["start_ms"])
    planning_end_ms = int(planning_segment["end_ms"])
    trajectory_contract = candidate_trajectory_contract(slots)
    result: list[dict[str, Any]] = []
    rejections: list[dict[str, Any]] = []
    for trajectory_index, raw_trajectory in enumerate(raw_trajectories, 1):
        trajectory_id = f"{group_id}_trajectory_{trajectory_index:02d}"
        try:
            if not isinstance(raw_trajectory, dict):
                raise ValueError("Trajectory must be an object")
            raw_items = raw_trajectory.get("items")
            if not isinstance(raw_items, list):
                raise ValueError("Trajectory must contain an items array")
            raw_slot_ids = [
                str(item.get("slot_id") or "")
                for item in raw_items
                if isinstance(item, dict)
            ]
            if (
                len(raw_slot_ids) != len(raw_items)
                or raw_slot_ids != expected_slot_ids
            ):
                raise ValueError(
                    f"Trajectory for {group_id} must cover Slots in exact group order"
                )
            trajectory_contract.validate_structure(raw_trajectory)
            proposed_starts: list[int] = []
            planned_durations: list[int] = []
            for slot, raw in zip(slots, raw_items, strict=True):
                slot_id = str(slot["slot_id"])
                source_start_ms = raw.get("source_start_ms")
                if isinstance(source_start_ms, bool) or not isinstance(
                    source_start_ms,
                    int,
                ):
                    raise ValueError(
                        f"Candidate for {slot_id} has invalid source_start_ms"
                    )
                planned_duration_ms = slot.get("planned_duration_ms")
                if (
                    isinstance(planned_duration_ms, bool)
                    or not isinstance(planned_duration_ms, int)
                    or planned_duration_ms <= 0
                ):
                    raise ValueError(
                        f"Slot {slot_id} has invalid planned_duration_ms"
                    )
                proposed_starts.append(source_start_ms)
                planned_durations.append(planned_duration_ms)
            normalized_ranges = _normalize_candidate_ranges(
                proposed_starts,
                planned_durations,
                planning_start_ms=planning_start_ms,
                planning_end_ms=planning_end_ms,
            )

            items: list[dict[str, Any]] = []
            for slot, raw, candidate_range in zip(
                slots,
                raw_items,
                normalized_ranges,
                strict=True,
            ):
                slot_id = str(slot["slot_id"])
                start_ms, end_ms = candidate_range
                source_shot_ids = _source_shot_ids_for_range(
                    source_segment,
                    candidate_range,
                )
                if not source_shot_ids:
                    raise ValueError(
                        f"Candidate for {slot_id} does not overlap a Shot"
                    )
                description = raw["description"].strip()
                if not description:
                    raise ValueError(
                        f"Candidate for {slot_id} has no visual description"
                    )
                candidate_id = f"{trajectory_id}_{slot_id}"
                items.append(
                    {
                        "candidate_id": candidate_id,
                        "trajectory_id": trajectory_id,
                        "group_id": group_id,
                        "planning_segment_id": planning_segment_id,
                        "planning_segment_start_ms": planning_start_ms,
                        "planning_segment_end_ms": planning_end_ms,
                        "slot_id": slot_id,
                        "timestamp": format_range(
                            start_ms / 1000.0,
                            end_ms / 1000.0,
                        ),
                        "source_segment_id": source_segment_id,
                        "source_shot_ids": source_shot_ids,
                        "structured_context": description,
                        "description": description,
                        "semantic_relevance": _bounded_score(
                            raw["semantic_relevance"],
                            "semantic_relevance",
                        ),
                        "emotional_intensity": _bounded_score(
                            raw["emotional_intensity"],
                            "emotional_intensity",
                        ),
                        "salience": _bounded_score(
                            raw["salience"],
                            "salience",
                        ),
                    }
                )
            result.append(
                {
                    "trajectory_id": trajectory_id,
                    "group_id": group_id,
                    "planning_segment_id": planning_segment_id,
                    "source_segment_id": source_segment_id,
                    "items": items,
                }
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            failure = build_prompt_failure(
                PromptFailureCode.RESPONSE_VALIDATION_FAILED,
                error_message=error_summary(exc),
            )
            rejections.append(
                {
                    **failure,
                    "group_id": group_id,
                    "trajectory_id": trajectory_id,
                    "trajectory_index": trajectory_index,
                    "error_type": type(exc).__name__,
                }
            )
    if raw_trajectories and not result:
        diagnoses = "; ".join(
            str(rejection.get("diagnosis") or "unknown validation error")
            for rejection in rejections
        )
        raise ValueError(
            "Every trajectory failed response validation"
            + (f": {diagnoses}" if diagnoses else "")
        )
    return {"trajectories": result, "rejections": rejections}


def _validate_visual_grounding(
    parsed: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    expected = {str(candidate["candidate_id"]) for candidate in candidates}
    raw_items = parsed.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("Visual validation response must contain an items array")
    result: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("Visual validation item must be an object")
        candidate_id = str(raw.get("candidate_id") or "")
        if candidate_id not in expected or candidate_id in result:
            raise ValueError(f"Unexpected visual candidate ID: {candidate_id}")
        description = str(raw.get("visible_description") or "").strip()
        if not description:
            raise ValueError(f"Missing visible description for {candidate_id}")
        visibility = float(raw["required_subject_visibility"])
        relevance = float(raw["visual_slot_relevance"])
        if not visibility.is_integer() or not 1 <= visibility <= 5:
            raise ValueError(
                f"required_subject_visibility for {candidate_id} must be 1 to 5"
            )
        if not relevance.is_integer() or not 1 <= relevance <= 5:
            raise ValueError(
                f"visual_slot_relevance for {candidate_id} must be 1 to 5"
            )
        result[candidate_id] = {
            "description": description,
            "visible_subjects": [
                str(value).strip()
                for value in raw.get("visible_subjects") or []
                if str(value).strip()
            ],
            "protagonist_visibility_likert": int(visibility),
            "visual_slot_relevance_likert": int(relevance),
            "visual_evidence": str(raw.get("visual_evidence") or "").strip(),
        }
    if set(result) != expected:
        raise ValueError(f"Visual validation omitted IDs: {sorted(expected - set(result))}")
    return result


def _candidate_segment_video_descriptions(
    candidate: dict[str, Any],
    video_description: dict[str, Any],
) -> list[dict[str, Any]]:
    candidate_range = _range_ms(str(candidate["timestamp"]))
    source_segment_id = str(candidate["source_segment_id"])
    result: list[dict[str, Any]] = []
    for segment in video_description["segments"]:
        if str(segment["segment_id"]) != source_segment_id:
            continue
        segment_context = {
            key: segment[key]
            for key in (
                "segment_id",
                "time_range",
                "content_type",
                "timeline_role",
                "segment_summary",
                "narrative_function",
                "emotional_tone",
                "appearing_characters",
            )
            if key in segment
        }
        segment_context["overlapping_shots"] = [
            {
                key: shot[key]
                for key in (
                    "shot_id",
                    "time_range",
                    "visual_description",
                    "dominant_action",
                    "scene",
                    "characters",
                    "shot_scale",
                    "camera_angle",
                    "camera_movement",
                    "composition",
                    "visual_evidence",
                    "visual_annotation_status",
                    "visual_annotation_failure",
                )
                if key in shot
            }
            for shot in segment["shots"]
            if _ranges_overlap(
                candidate_range,
                (
                    _seconds_to_ms(shot["time_range"]["start_sec"]),
                    _seconds_to_ms(shot["time_range"]["end_sec"]),
                ),
            )
        ]
        candidate_dialogues: dict[str, dict[str, Any]] = {}
        for shot in segment["shots"]:
            for dialogue in shot.get("dialogue", []):
                dialogue_range = (
                    _seconds_to_ms(dialogue["time_range"]["start_sec"]),
                    _seconds_to_ms(dialogue["time_range"]["end_sec"]),
                )
                if not _ranges_overlap(candidate_range, dialogue_range):
                    continue
                dialogue_id = str(dialogue["dialogue_id"])
                candidate_dialogues[dialogue_id] = {
                    key: dialogue[key]
                    for key in (
                        "dialogue_id",
                        "time_range",
                        "speaker",
                        "text",
                        "speech_mode",
                    )
                    if key in dialogue
                }
        segment_context["candidate_dialogue"] = sorted(
            candidate_dialogues.values(),
            key=lambda dialogue: _seconds_to_ms(
                dialogue["time_range"]["start_sec"]
            ),
        )
        result.append(segment_context)
    return result


def add_visual_features(
    media: SegmentMediaReader,
    slots: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    config: VLMConfig,
    context: WorkflowContext,
    *,
    sample_frames: int,
    operation: str,
) -> None:
    if not candidates:
        return
    with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as executor:
        image_urls = list(
            executor.map(
                lambda candidate: _contact_sheet_data_url(
                    media,
                    candidate,
                    sample_frames,
                ),
                candidates,
            )
        )
    slots_by_id = {str(slot["slot_id"]): slot for slot in slots}
    video_description = context.get_artifact("video_description")
    if not isinstance(video_description, dict):
        raise RuntimeError("Video description is required for visual validation")

    def candidate_spec(candidate: dict[str, Any]) -> dict[str, Any]:
        slot = slots_by_id[str(candidate["slot_id"])]
        return {
            "candidate_id": candidate["candidate_id"],
            "slot_id": candidate["slot_id"],
            "intended_visible_content": slot["content_description"],
            "required_visible_subjects": list(
                slot.get("required_visible_subjects") or []
            ),
            "source_segment_video_descriptions": (
                _candidate_segment_video_descriptions(
                    candidate,
                    video_description,
                )
            ),
        }

    def score_subset(
        subset: list[dict[str, Any]],
        subset_urls: list[str],
        suffix: str = "",
        *,
        resampled: bool = False,
    ) -> dict[str, dict[str, Any]]:
        subset_operation = operation + suffix
        package = prompt_registry.build(
            PromptStage.PLANNERS,
            PromptTask.CANDIDATE_VISUAL_SCORING,
            CandidateVisualScoringDetails(
                operation=subset_operation,
                candidates=[candidate_spec(candidate) for candidate in subset],
            ),
        )
        try:
            return context.call_prompt(
                package=package,
                config=config,
                validate_business=lambda parsed: _validate_visual_grounding(
                    parsed,
                    subset,
                ),
                image_data_urls=subset_urls,
                image_labels=[str(candidate["candidate_id"]) for candidate in subset],
            )
        except Exception as exc:
            error_text = str(exc).lower()
            inspection_failed = "data_inspection_failed" in error_text
            payload_limit = (
                "exceeded limit on max data-uri per request" in error_text
            )
            if not inspection_failed and not payload_limit:
                raise
            failure = build_prompt_failure(
                (
                    PromptFailureCode.PROVIDER_REQUEST_LIMIT_EXCEEDED
                    if payload_limit
                    else PromptFailureCode.PROVIDER_IMAGE_INSPECTION_FAILED
                ),
                operation=subset_operation,
                error_message=error_summary(exc),
            )
            if len(subset) > 1:
                midpoint = len(subset) // 2
                log_event(
                    "WARNING",
                    "aster.timeline",
                    "fallback.apply",
                    "Image batch could not be processed; splitting trajectory items",
                    candidates=len(subset),
                    **failure,
                )
                return {
                    **score_subset(
                        subset[:midpoint],
                        subset_urls[:midpoint],
                        f"{suffix} split A",
                    ),
                    **score_subset(
                        subset[midpoint:],
                        subset_urls[midpoint:],
                        f"{suffix} split B",
                    ),
                }
            if inspection_failed and not resampled:
                candidate = subset[0]
                log_event(
                    "WARNING",
                    "aster.timeline",
                    "fallback.apply",
                    "Image was rejected; resampling one frame",
                    candidate_id=candidate["candidate_id"],
                    **failure,
                )
                return score_subset(
                    subset,
                    [_contact_sheet_data_url(media, candidate, 1)],
                    f"{suffix} resampled",
                    resampled=True,
                )
            raise

    grounded = score_subset(candidates, image_urls)
    for candidate in candidates:
        candidate.update(grounded[str(candidate["candidate_id"])])


def _candidate_motion(
    media: SegmentMediaReader,
    start: float,
    end: float,
    fps: float,
) -> float:
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise ValueError("Motion sampling requires a finite positive source range")
    if not math.isfinite(fps) or fps <= 0.0:
        raise ValueError("Motion sampling requires a finite positive sample rate")
    values: list[float] = []
    step = 1.0 / max(fps, 0.1)
    sample_times: list[float] = []
    next_sample = start
    while next_sample < end:
        sample_times.append(next_sample)
        next_sample += step
    if len(sample_times) < 2:
        midpoint = start + (end - start) / 2.0
        if not start < midpoint < end:
            raise RuntimeError("Source range cannot provide two distinct motion samples")
        sample_times.append(midpoint)
    frames = media.sample_frames(sample_times)
    if frames is None or len(frames) != len(sample_times):
        raise RuntimeError(
            "Motion sampling did not decode every requested frame"
        )
    previous = None
    for frame in frames:
        if frame is None or getattr(frame, "size", 0) == 0:
            raise RuntimeError("Motion sampling returned an invalid frame")
        gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
        if previous is not None:
            values.append(float(cv2.absdiff(gray, previous).mean()) / 255.0)
        previous = gray
    if not values:
        raise RuntimeError("Motion sampling requires at least two valid frames")
    trimmed = list(values)
    if len(trimmed) >= 3:
        trimmed.remove(max(trimmed))
    raw = sum(trimmed) / len(trimmed)
    return max(0.0, min(1.0, raw / 0.18))


def add_kinetic_features(
    media: SegmentMediaReader,
    candidates: list[dict[str, Any]],
    sample_fps: float,
    max_workers: int = 4,
    *,
    show_progress: bool = False,
) -> None:
    if not candidates:
        return
    worker_count = max(1, min(max_workers, len(candidates)))
    progress = (
        progress_bar(
            total=len(candidates),
            description="Candidate motion analysis",
            unit="candidate",
        )
        if show_progress
        else None
    )

    def process(candidate: dict[str, Any]) -> None:
        start, end = parse_range(str(candidate["timestamp"]))
        candidate["kinetic_energy"] = round(
            _candidate_motion(media, start, end, sample_fps),
            4,
        )
        if progress is not None:
            progress.update()

    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            list(executor.map(process, candidates))
    finally:
        if progress is not None:
            progress.close()


def _validate_complete_trajectory(
    trajectory: dict[str, Any],
    slots: list[dict[str, Any]],
    media: SegmentMediaReader,
    vlm_config: VLMConfig,
    retrieval_config: CandidateRetrievalConfig,
    context: WorkflowContext,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Accept or reject one trajectory as an indivisible unit."""

    items = [dict(item) for item in trajectory["items"]]
    rejected: list[dict[str, Any]] = []
    add_kinetic_features(
        media,
        items,
        retrieval_config.motion_sample_fps,
        retrieval_config.motion_workers,
    )
    threshold = retrieval_config.static_kinetic_energy_threshold
    for slot, item in zip(slots, items, strict=True):
        if float(item["kinetic_energy"]) > threshold:
            continue
        rejected.append(
            {
                **build_prompt_failure(
                    PromptFailureCode.VISUALLY_STATIC,
                    candidate_id=item["candidate_id"],
                    timestamp=item["timestamp"],
                    kinetic_energy=item["kinetic_energy"],
                    static_threshold=threshold,
                ),
                "slot_id": str(slot["slot_id"]),
                "group_id": str(trajectory["group_id"]),
                "diagnostic_source": "local_motion",
                "planned_content_description": slot["content_description"],
                "required_visible_subjects": list(
                    slot.get("required_visible_subjects") or []
                ),
                "source_segment_id": item["source_segment_id"],
                "planning_segment_id": item.get("planning_segment_id"),
                "source_shot_ids": list(item.get("source_shot_ids") or []),
                "candidate_description": item.get(
                    "structured_context", item["description"]
                ),
            }
        )
    if rejected:
        return None, rejected
    add_visual_features(
        media,
        slots,
        items,
        vlm_config,
        context,
        sample_frames=retrieval_config.visual_sample_frames,
        operation=f"Visual trajectory validation group {trajectory['group_id']}",
    )
    for slot, item in zip(slots, items, strict=True):
        required_subjects = list(slot.get("required_visible_subjects") or [])
        visibility = int(item["protagonist_visibility_likert"])
        if (
            required_subjects
            and visibility
            < retrieval_config.protagonist_visibility_likert_threshold
        ):
            rejected.append(
                {
                    **build_prompt_failure(
                        PromptFailureCode.REQUIRED_SUBJECT_NOT_VISUALLY_CONFIRMED,
                        candidate_id=item["candidate_id"],
                        timestamp=item["timestamp"],
                        required_visible_subjects=required_subjects,
                        visual_evidence=item["visual_evidence"],
                    ),
                    "slot_id": str(slot["slot_id"]),
                    "group_id": str(trajectory["group_id"]),
                    "diagnostic_source": "visual_grounding",
                    "planned_content_description": slot["content_description"],
                    "required_visible_subjects": required_subjects,
                    "source_segment_id": item["source_segment_id"],
                    "planning_segment_id": item.get("planning_segment_id"),
                    "source_shot_ids": list(item.get("source_shot_ids") or []),
                    "candidate_description": item.get(
                        "structured_context", item["description"]
                    ),
                    "visible_description": item["description"],
                    "visible_subjects": list(item.get("visible_subjects") or []),
                    "protagonist_visibility_likert": visibility,
                    "protagonist_visibility_likert_threshold": (
                        retrieval_config.protagonist_visibility_likert_threshold
                    ),
                }
            )
    if rejected:
        return None, rejected
    for slot, item in zip(slots, items, strict=True):
        item["selection_score"] = round(score_unary_candidate(slot, item), 6)
    accepted = dict(trajectory)
    accepted["items"] = items
    accepted["selection_score"] = round(
        sum(float(item["selection_score"]) for item in items) / len(items),
        6,
    )
    return accepted, []


def retrieve_candidates(
    slots: list[dict[str, Any]],
    media: SegmentMediaReader,
    config: LLMConfig,
    vlm_config: VLMConfig,
    retrieval_config: CandidateRetrievalConfig,
    context: WorkflowContext,
    cancellation_token: CancellationToken | None = None,
    *,
    target_group_ids: set[str] | None = None,
    seed_candidate_pool: dict[str, list[dict[str, Any]]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Retrieve and validate exactly one trajectory batch per targeted group."""

    raise_if_cancelled(cancellation_token)
    units = _planning_units(slots, context)
    ordered_group_ids = [
        str(unit["group"]["group_id"])
        for unit in units
    ]
    all_group_ids = set(ordered_group_ids)
    retrieval_group_ids = (
        set(all_group_ids)
        if target_group_ids is None
        else {str(group_id) for group_id in target_group_ids}
    )
    unknown_group_ids = retrieval_group_ids - all_group_ids
    if unknown_group_ids:
        raise ValueError(
            "Candidate retrieval targets unknown Planning Groups: "
            + ", ".join(sorted(unknown_group_ids))
        )
    seed = seed_candidate_pool or {}
    pool: dict[str, list[dict[str, Any]]] = {}
    for group_id in ordered_group_ids:
        if group_id in retrieval_group_ids:
            pool[group_id] = []
            continue
        trajectories = seed.get(group_id)
        if not isinstance(trajectories, list) or not trajectories:
            raise ValueError(
                f"Candidate retrieval seed is missing Planning Group {group_id}"
            )
        pool[group_id] = [dict(trajectory) for trajectory in trajectories]
    extra_seed_group_ids = set(seed) - all_group_ids
    if extra_seed_group_ids:
        raise ValueError(
            "Candidate retrieval seed contains unknown Planning Groups: "
            + ", ".join(sorted(extra_seed_group_ids))
        )
    context.set_artifact("candidate_pool", pool)
    rejected: list[dict[str, Any]] = []
    failures_by_group: dict[str, list[dict[str, Any]]] = {
        group_id: [] for group_id in retrieval_group_ids
    }
    video_description = context.get_artifact("video_description")
    if not isinstance(video_description, dict):
        raise RuntimeError("Video description is required for candidate retrieval")
    previous_feedback = context.get_artifact("planners_feedback") or {}
    candidate_failure_evidence = (
        previous_feedback.get("candidate_failure_evidence") or []
    )

    unit_inputs: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for unit in units:
        group_id = str(unit["group"]["group_id"])
        unit_inputs[group_id] = _source_segment_context(
            unit["planning_segment"],
            video_description,
        )

    target = retrieval_config.target_trajectories_per_group
    target_units = [
        unit
        for unit in units
        if str(unit["group"]["group_id"]) in retrieval_group_ids
    ]

    def retrieve_one(
        unit: dict[str, Any],
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
        raise_if_cancelled(cancellation_token)
        group = unit["group"]
        group_id = str(group["group_id"])
        slot_ids = {str(slot["slot_id"]) for slot in unit["slots"]}
        compact_segment, source_segment = unit_inputs[group_id]
        requested_count = _trajectory_batch_size(
            unit["slots"],
            unit["planning_segment"],
            target,
        )
        package = prompt_registry.build(
            PromptStage.PLANNERS,
            PromptTask.CANDIDATE_RETRIEVAL,
            CandidateRetrievalDetails(
                operation=f"Candidate trajectory retrieval group {group_id}",
                trajectories_per_group=requested_count,
                group=group,
                slots=unit["slots"],
                planning_segment=compact_segment,
                rejection_feedback=[
                    dict(evidence)
                    for evidence in candidate_failure_evidence
                    if slot_ids.intersection(
                        str(value) for value in evidence.get("slot_ids") or []
                    )
                ],
            ),
        )
        response_result = context.call_prompt(
            package=package,
            config=config,
            validate_business=lambda parsed: _validate_trajectory_response(
                parsed,
                group=group,
                slots=unit["slots"],
                planning_segment=unit["planning_segment"],
                source_segment=source_segment,
                requested_count=requested_count,
            ),
        )
        raise_if_cancelled(cancellation_token)
        return (
            group_id,
            response_result["trajectories"],
            response_result["rejections"],
        )

    retrieved: list[
        tuple[str, list[dict[str, Any]], list[dict[str, Any]]]
    ] = []
    if target_units:
        workers = max(1, min(config.max_concurrency, len(target_units)))
        log_event(
            "INFO",
            "aster.timeline",
            "stage.progress",
            "Group trajectory retrieval batch started",
            groups=len(target_units),
            workers=workers,
            target_trajectories_per_group=target,
        )
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="trajectory-llm",
        ) as executor:
            retrieved = list(executor.map(retrieve_one, target_units))
        raise_if_cancelled(cancellation_token)

    units_by_group_id = {
        str(unit["group"]["group_id"]): unit for unit in target_units
    }
    for group_id, _trajectories, response_rejections in retrieved:
        rejected.extend(response_rejections)
        failures_by_group[group_id].extend(response_rejections)
        for rejection in response_rejections:
            log_event(
                "WARNING",
                "aster.timeline",
                "validation.reject",
                "One trajectory response item failed validation",
                **rejection,
            )

    def duplicate_failures(
        trajectory: dict[str, Any],
        accepted_source_evidence: dict[str, list[dict[str, Any]]],
        unit: dict[str, Any],
    ) -> list[dict[str, Any]]:
        slots_by_id = {
            str(slot["slot_id"]): slot for slot in unit["slots"]
        }
        failures: list[dict[str, Any]] = []
        for item in trajectory["items"]:
            slot_id = str(item["slot_id"])
            conflicting = next(
                (
                    previous
                    for previous in accepted_source_evidence[slot_id]
                    if _candidate_duplicates_source_evidence(item, previous)
                ),
                None,
            )
            if conflicting is None:
                continue
            failure = build_prompt_failure(
                PromptFailureCode.DUPLICATE_CANDIDATE_RANGE,
                candidate_id=item["candidate_id"],
                timestamp=item["timestamp"],
                slot_id=slot_id,
            )
            failure.update(
                {
                    "slot_id": slot_id,
                    "group_id": str(trajectory["group_id"]),
                    "diagnostic_source": "source_evidence_diversity",
                    "planned_content_description": slots_by_id[slot_id][
                        "content_description"
                    ],
                    "required_visible_subjects": list(
                        slots_by_id[slot_id].get("required_visible_subjects") or []
                    ),
                    "source_segment_id": item["source_segment_id"],
                    "planning_segment_id": item.get("planning_segment_id"),
                    "source_shot_ids": list(item.get("source_shot_ids") or []),
                    "candidate_description": item.get(
                        "structured_context", item["description"]
                    ),
                    "conflicting_candidate_id": conflicting["candidate_id"],
                    "conflicting_timestamp": conflicting["timestamp"],
                }
            )
            failures.append(failure)
        return failures

    def validate_group(
        retrieval: tuple[str, list[dict[str, Any]], list[dict[str, Any]]],
    ) -> tuple[
        str,
        list[dict[str, Any]],
        list[dict[str, Any]],
        bool,
    ]:
        group_id, trajectories, response_rejections = retrieval
        unit = units_by_group_id[group_id]
        accepted_trajectories: list[dict[str, Any]] = []
        trajectory_rejections: list[dict[str, Any]] = []
        accepted_source_evidence = {
            str(slot["slot_id"]): [] for slot in unit["slots"]
        }
        all_items_static = not response_rejections and bool(trajectories)
        for trajectory in trajectories:
            raise_if_cancelled(cancellation_token)
            item_failures = duplicate_failures(
                trajectory,
                accepted_source_evidence,
                unit,
            )
            accepted: dict[str, Any] | None = None
            if not item_failures:
                accepted, item_failures = _validate_complete_trajectory(
                    trajectory,
                    unit["slots"],
                    media,
                    vlm_config,
                    retrieval_config,
                    context,
                )
            raise_if_cancelled(cancellation_token)
            if accepted is not None:
                accepted_trajectories.append(accepted)
                for item in accepted["items"]:
                    accepted_source_evidence[str(item["slot_id"])].append(item)
                all_items_static = False
                continue

            expected_candidate_ids = {
                str(item["candidate_id"]) for item in trajectory["items"]
            }
            static_candidate_ids = {
                str(failure.get("candidate_id") or "")
                for failure in item_failures
                if failure.get("reason_code") == PromptFailureCode.VISUALLY_STATIC
            }
            if (
                len(item_failures) != len(expected_candidate_ids)
                or static_candidate_ids != expected_candidate_ids
            ):
                all_items_static = False
            rejection = {
                **build_prompt_failure(
                    PromptFailureCode.NO_CANDIDATE_PASSED_VISUAL_DIAGNOSTICS,
                ),
                "group_id": group_id,
                "trajectory_id": trajectory["trajectory_id"],
                "candidate_rejections": item_failures,
            }
            trajectory_rejections.append(rejection)
        return (
            group_id,
            accepted_trajectories,
            trajectory_rejections,
            all_items_static,
        )

    validated_groups: list[
        tuple[str, list[dict[str, Any]], list[dict[str, Any]], bool]
    ] = []
    if retrieved:
        validation_workers = max(
            1,
            min(vlm_config.max_concurrency, len(retrieved)),
        )
        with ThreadPoolExecutor(
            max_workers=validation_workers,
            thread_name_prefix="trajectory-vlm",
        ) as executor:
            validated_groups = list(executor.map(validate_group, retrieved))
        raise_if_cancelled(cancellation_token)

    unavailable_source_segment_ids: set[str] = set()
    for (
        group_id,
        accepted_trajectories,
        trajectory_rejections,
        all_items_static,
    ) in validated_groups:
        pool[group_id] = accepted_trajectories
        rejected.extend(trajectory_rejections)
        failures_by_group[group_id].extend(trajectory_rejections)
        if not accepted_trajectories and all_items_static:
            unavailable_source_segment_ids.add(
                str(units_by_group_id[group_id]["group"]["source_segment_id"])
            )
        for rejection in trajectory_rejections:
            log_event(
                "WARNING",
                "aster.timeline",
                "validation.reject",
                "Complete trajectory rejected because one or more items failed",
                **rejection,
            )

    context.set_artifact("candidate_rejections", rejected)
    context.set_artifact("candidate_pool", pool)
    counts = {group_id: len(values) for group_id, values in pool.items()}
    summary = {
        "valid_trajectory_counts": counts,
        "viable_trajectory_counts": _viable_trajectory_counts(slots, pool),
        "target_trajectories_per_group": target,
        # All targeted groups are fetched concurrently in one workflow batch.
        # ``len(retrieved)`` is the number of groups, not a retry/batch count.
        "retrieval_batches_completed": 1 if retrieved else 0,
        "underfilled_group_ids": sorted(
            group_id
            for group_id, count in counts.items()
            if 0 < count < target
        ),
    }
    context.set_artifact("retrieval_summary", summary)
    failed_group_ids = sorted(
        group_id
        for group_id in retrieval_group_ids
        if counts.get(group_id, 0) == 0
    )
    if failed_group_ids:
        failure = build_prompt_failure(
            PromptFailureCode.INSUFFICIENT_VISUALLY_GROUNDED_CANDIDATES,
            shortages={group_id: target for group_id in failed_group_ids},
        )
        diagnostics = {
            **failure,
            **summary,
            "failed_group_ids": failed_group_ids,
            "semantic_zero_candidate_group_ids": failed_group_ids,
            "unavailable_source_segment_ids": sorted(
                unavailable_source_segment_ids
            ),
            "group_failures": {
                group_id: failures_by_group[group_id]
                for group_id in failed_group_ids
            },
            "candidate_rejections": rejected,
        }
        context.set_artifact("retrieval_failure", diagnostics)
        raise GroupNoCandidateError(diagnostics)

    log_event(
        "INFO",
        "aster.timeline",
        "stage.complete",
        "Group trajectory retrieval batch completed",
        **summary,
    )
    return pool


class TimelineScoutAgent:
    """T agent: retrieve and validate indivisible Slot Group trajectories."""

    def __init__(
        self,
        media: SegmentMediaReader,
        config: AppConfig,
        context: WorkflowContext,
    ) -> None:
        self.media = media
        self.config = config
        self.context = context

    def scout(
        self,
        slots: list[dict[str, Any]],
        cancellation_token: CancellationToken | None = None,
        *,
        target_group_ids: set[str] | None = None,
        seed_candidate_pool: dict[str, list[dict[str, Any]]] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        return retrieve_candidates(
            slots,
            self.media,
            self.config.llm,
            self.config.vlm,
            self.config.planners.candidate_retrieval,
            self.context,
            cancellation_token,
            target_group_ids=target_group_ids,
            seed_candidate_pool=seed_candidate_pool,
        )


__all__ = ["TimelineScoutAgent"]
