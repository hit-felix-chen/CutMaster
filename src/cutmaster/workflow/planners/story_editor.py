from __future__ import annotations

from dataclasses import replace
from itertools import combinations
from typing import Any

from cutmaster.configuration.schema import AppConfig, DialogueAnchorConfig, LLMConfig
from cutmaster.infrastructure.observability.logging import log_event
from cutmaster.workflow.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.workflow.prompting.planners import DialogueAnchorSelectionDetails
from cutmaster.workflow.shared.execution_context import WorkflowContext
from cutmaster.workflow.shared.timecode import format_range, parse_range


_TOLERANCE_SEC = 0.001


def _require_milliseconds(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer number of milliseconds")
    return value


def _seconds_to_ms(value: Any) -> int:
    return int(round(float(value) * 1000.0))


def _time_range_ms(value: Any) -> tuple[int, int]:
    start_sec, end_sec = parse_range(str(value))
    return _seconds_to_ms(start_sec), _seconds_to_ms(end_sec)


def _slot_duration_ms(slot: dict[str, Any]) -> int:
    return _require_milliseconds(
        slot.get("planned_duration_ms"),
        f"{slot.get('slot_id', 'Slot')} planned_duration_ms",
    )


def _source_segment_id(slot: dict[str, Any]) -> str:
    segment_id = str(slot.get("source_segment_id") or "").strip()
    if not segment_id:
        raise ValueError(f"{slot.get('slot_id', 'Slot')} has no source_segment_id")
    return segment_id


def _base_group_id(slot: dict[str, Any]) -> str:
    group_id = str(
        slot.get("parent_group_id") or slot.get("group_id") or ""
    ).strip()
    if not group_id:
        raise ValueError(f"{slot.get('slot_id', 'Slot')} has no group_id")
    return group_id


def _normalise_arrangement_slots(
    slots: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Restore the A-stage group identity before selecting anchors again."""

    result: list[dict[str, Any]] = []
    for slot in slots:
        item = {
            key: value
            for key, value in slot.items()
            if key
            not in {
                "dialogue_anchor",
                "fixed_candidate",
                "parent_group_id",
                "planning_segment_id",
            }
        }
        item["group_id"] = _base_group_id(slot)
        item["source_segment_id"] = _source_segment_id(slot)
        planned_duration_ms = _slot_duration_ms(slot)
        item["planned_duration_ms"] = planned_duration_ms
        item["planned_duration_sec"] = round(planned_duration_ms / 1000.0, 6)
        result.append(item)
    return result


def _same_arrangement_group(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
) -> bool:
    """Compare group planning fields while ignoring derived output placement."""

    def without_output_placement(slot: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in slot.items()
            if key not in {"output_start_sec", "output_end_sec"}
        }

    return [without_output_placement(slot) for slot in previous] == [
        without_output_placement(slot) for slot in current
    ]


def _grouped_slots(
    slots: list[dict[str, Any]],
) -> list[tuple[str, str, list[dict[str, Any]]]]:
    groups: list[tuple[str, str, list[dict[str, Any]]]] = []
    closed_group_ids: set[str] = set()
    for slot in slots:
        group_id = _base_group_id(slot)
        segment_id = _source_segment_id(slot)
        if groups and groups[-1][0] == group_id:
            if groups[-1][1] != segment_id:
                raise ValueError(
                    f"Group {group_id} must bind exactly one source_segment_id"
                )
            groups[-1][2].append(slot)
            continue
        if group_id in closed_group_ids:
            raise ValueError(f"Group {group_id} must be one contiguous Slot run")
        if groups:
            closed_group_ids.add(groups[-1][0])
        groups.append((group_id, segment_id, [slot]))
    return groups


def _segment_bounds_ms(segment: dict[str, Any]) -> tuple[int, int]:
    time_range = segment["time_range"]
    start_ms = _seconds_to_ms(time_range["start_sec"])
    end_ms = _seconds_to_ms(time_range["end_sec"])
    if end_ms <= start_ms:
        raise ValueError(f"Segment {segment['segment_id']} has invalid time bounds")
    return start_ms, end_ms


def _preferred_anchor_picture_range(
    *,
    source_segment_id: str,
    segment_start_ms: int,
    segment_end_ms: int,
    slot_index: int,
    slot_count: int,
) -> dict[str, Any]:
    """Return the count-based soft picture range for one grouped Slot."""

    duration_ms = segment_end_ms - segment_start_ms
    start_ms = segment_start_ms + duration_ms * slot_index // slot_count
    end_ms = segment_start_ms + duration_ms * (slot_index + 1) // slot_count
    return {
        "source_segment_id": source_segment_id,
        "start_sec": round(start_ms / 1000.0, 6),
        "end_sec": round(end_ms / 1000.0, 6),
        "left_slot_count": slot_index,
        "right_slot_count": slot_count - slot_index - 1,
    }


def _build_partition_layout(
    slots: list[dict[str, Any]],
    anchor_ranges_by_slot: dict[str, tuple[int, int]],
    video_description: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build deterministic free-picture regions and validate their capacity."""

    segments = {
        str(segment["segment_id"]): segment
        for segment in video_description["segments"]
    }
    planning_segment_counts: dict[str, int] = {}
    layout: list[dict[str, Any]] = []
    for parent_group_id, source_segment_id, group_slots in _grouped_slots(slots):
        segment = segments.get(source_segment_id)
        if segment is None:
            raise ValueError(
                f"Group {parent_group_id} references unknown Segment "
                f"{source_segment_id}"
            )
        segment_start_ms, segment_end_ms = _segment_bounds_ms(segment)
        anchors: list[dict[str, Any]] = []
        for slot_index, slot in enumerate(group_slots):
            slot_id = str(slot["slot_id"])
            picture_range = anchor_ranges_by_slot.get(slot_id)
            if picture_range is None:
                continue
            picture_start_ms, picture_end_ms = picture_range
            if (
                picture_start_ms < segment_start_ms
                or picture_end_ms > segment_end_ms
                or picture_end_ms <= picture_start_ms
            ):
                raise ValueError(
                    f"Anchor {slot_id} picture window exceeds source Segment "
                    f"{source_segment_id}"
                )
            if picture_end_ms - picture_start_ms != _slot_duration_ms(slot):
                raise ValueError(
                    f"Anchor {slot_id} picture window must equal its planned duration"
                )
            anchors.append(
                {
                    "slot_id": slot_id,
                    "slot_index": slot_index,
                    "start_ms": picture_start_ms,
                    "end_ms": picture_end_ms,
                }
            )

        previous_anchor: dict[str, Any] | None = None
        for anchor in anchors:
            if previous_anchor is not None and (
                int(anchor["start_ms"]) <= int(previous_anchor["start_ms"])
                or int(anchor["start_ms"]) < int(previous_anchor["end_ms"])
            ):
                raise ValueError(
                    "Anchor picture windows must be strictly increasing and "
                    "non-overlapping in Slot order"
                )
            previous_anchor = anchor

        free_runs: list[dict[str, Any]] = []
        source_cursor_ms = segment_start_ms
        slot_cursor = 0
        for anchor in anchors:
            anchor_slot_index = int(anchor["slot_index"])
            ordinary_slots = group_slots[slot_cursor:anchor_slot_index]
            if ordinary_slots:
                free_runs.append(
                    {
                        "slots": ordinary_slots,
                        "start_ms": source_cursor_ms,
                        "end_ms": int(anchor["start_ms"]),
                    }
                )
            source_cursor_ms = int(anchor["end_ms"])
            slot_cursor = anchor_slot_index + 1
        trailing_slots = group_slots[slot_cursor:]
        if trailing_slots:
            free_runs.append(
                {
                    "slots": trailing_slots,
                    "start_ms": source_cursor_ms,
                    "end_ms": segment_end_ms,
                }
            )

        child_groups: list[dict[str, Any]] = []
        for child_index, free_run in enumerate(free_runs, 1):
            ordinary_slots = list(free_run["slots"])
            required_ms = sum(_slot_duration_ms(slot) for slot in ordinary_slots)
            available_ms = int(free_run["end_ms"]) - int(free_run["start_ms"])
            child_group_id = f"{parent_group_id}_{child_index:02d}"
            if required_ms > available_ms:
                raise ValueError(
                    "Anchor partition leaves an infeasible child group: "
                    f"failed_group_id={child_group_id} "
                    f"available_ms={available_ms} required_ms={required_ms}"
                )
            planning_segment_counts[source_segment_id] = (
                planning_segment_counts.get(source_segment_id, 0) + 1
            )
            planning_segment_id = (
                f"{source_segment_id}_"
                f"{planning_segment_counts[source_segment_id]:02d}"
            )
            child_groups.append(
                {
                    "group_id": child_group_id,
                    "parent_group_id": parent_group_id,
                    "source_segment_id": source_segment_id,
                    "planning_segment_id": planning_segment_id,
                    "slot_ids": [str(slot["slot_id"]) for slot in ordinary_slots],
                    "start_ms": int(free_run["start_ms"]),
                    "end_ms": int(free_run["end_ms"]),
                }
            )
        layout.append(
            {
                "parent_group_id": parent_group_id,
                "source_segment_id": source_segment_id,
                "anchors": anchors,
                "child_groups": child_groups,
            }
        )
    return layout


def _anchor_partition_balance_error_ms(
    slots: list[dict[str, Any]],
    anchor_ranges_by_slot: dict[str, tuple[int, int]],
    video_description: dict[str, Any],
) -> int:
    """Measure how unevenly Anchor pictures divide their source Segments."""

    segments = {
        str(segment["segment_id"]): segment
        for segment in video_description["segments"]
    }
    total_error_ms = 0
    for _group_id, source_segment_id, group_slots in _grouped_slots(slots):
        if len(group_slots) <= 1:
            continue
        anchor_ranges = [
            anchor_ranges_by_slot[str(slot["slot_id"])]
            for slot in group_slots
            if str(slot["slot_id"]) in anchor_ranges_by_slot
        ]
        if not anchor_ranges:
            continue
        segment_start_ms, segment_end_ms = _segment_bounds_ms(
            segments[source_segment_id]
        )
        free_region_lengths_ms: list[int] = []
        cursor_ms = segment_start_ms
        for anchor_start_ms, anchor_end_ms in anchor_ranges:
            free_region_lengths_ms.append(anchor_start_ms - cursor_ms)
            cursor_ms = anchor_end_ms
        free_region_lengths_ms.append(segment_end_ms - cursor_ms)
        total_error_ms += max(free_region_lengths_ms) - min(
            free_region_lengths_ms
        )
    return total_error_ms


def _overlaps(
    first: tuple[float, float],
    second: tuple[float, float],
) -> bool:
    return first[0] < second[1] - _TOLERANCE_SEC and second[0] < first[1] - _TOLERANCE_SEC


def _output_audio_range(anchor: dict[str, Any]) -> tuple[float, float]:
    return (
        float(anchor["output_audio_start_sec"]),
        float(anchor["output_audio_end_sec"]),
    )


def _audio_ranges_overlap(
    first: dict[str, Any],
    second: dict[str, Any],
) -> bool:
    return _overlaps(_output_audio_range(first), _output_audio_range(second))


def _audio_overlap_error(
    first: dict[str, Any],
    second: dict[str, Any],
) -> ValueError:
    first_start, first_end = _output_audio_range(first)
    second_start, second_end = _output_audio_range(second)
    return ValueError(
        "Dialogue-anchor L-cut audio ranges overlap: "
        f"{first['slot_id']} [{first_start:.3f}, {first_end:.3f}) "
        f"conflicts with {second['slot_id']} "
        f"[{second_start:.3f}, {second_end:.3f})"
    )


def _filter_constraints_against_preserved(
    dialogue_constraints_by_slot: dict[str, dict[str, Any]],
    preserved_anchors: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Hide prompt choices that cannot coexist with immutable Anchors."""

    filtered: dict[str, dict[str, Any]] = {}
    exclusions: list[dict[str, Any]] = []
    for slot_id, constraint in dialogue_constraints_by_slot.items():
        endpoint_map: dict[str, dict[str, list[str]]] = {}
        output_end_map: dict[str, dict[str, dict[str, float]]] = {}
        allowed_segment_ids: list[str] = []
        output_start_sec = float(constraint["output_audio_start_sec"])
        allowed_by_segment = constraint.get(
            "allowed_last_dialogue_ids_by_segment_and_first"
        ) or {}
        output_ends_by_segment = constraint.get(
            "output_audio_end_sec_by_segment_and_first_and_last"
        ) or {}
        for raw_segment_id, allowed_by_first in allowed_by_segment.items():
            segment_id = str(raw_segment_id)
            output_ends_by_first = output_ends_by_segment.get(segment_id) or {}
            for raw_first_id, raw_last_ids in allowed_by_first.items():
                first_dialogue_id = str(raw_first_id)
                output_ends_by_last = (
                    output_ends_by_first.get(first_dialogue_id) or {}
                )
                for raw_last_id in raw_last_ids:
                    last_dialogue_id = str(raw_last_id)
                    raw_output_end_sec = output_ends_by_last.get(last_dialogue_id)
                    if not isinstance(raw_output_end_sec, (int, float)):
                        raise ValueError(
                            f"Allowed dialogue endpoint {slot_id}/"
                            f"{segment_id}/{first_dialogue_id}/{last_dialogue_id} "
                            "has no output_audio_end_sec"
                        )
                    output_end_sec = float(raw_output_end_sec)
                    endpoint = {
                        "output_audio_start_sec": output_start_sec,
                        "output_audio_end_sec": output_end_sec,
                    }
                    conflicts = [
                        anchor
                        for anchor in preserved_anchors
                        if _audio_ranges_overlap(endpoint, anchor)
                    ]
                    if conflicts:
                        exclusions.append(
                            {
                                "reason_code": "preserved_anchor_audio_overlap",
                                "slot_id": str(slot_id),
                                "source_segment_id": segment_id,
                                "first_dialogue_id": first_dialogue_id,
                                "last_dialogue_id": last_dialogue_id,
                                "output_audio_start_sec": round(
                                    output_start_sec,
                                    6,
                                ),
                                "output_audio_end_sec": round(
                                    output_end_sec,
                                    6,
                                ),
                                "conflicts_with_preserved_anchors": [
                                    {
                                        "slot_id": str(anchor["slot_id"]),
                                        "output_audio_start_sec": round(
                                            float(anchor["output_audio_start_sec"]),
                                            6,
                                        ),
                                        "output_audio_end_sec": round(
                                            float(anchor["output_audio_end_sec"]),
                                            6,
                                        ),
                                    }
                                    for anchor in conflicts
                                ],
                            }
                        )
                        continue

                    if segment_id not in allowed_segment_ids:
                        allowed_segment_ids.append(segment_id)
                    endpoint_map.setdefault(segment_id, {}).setdefault(
                        first_dialogue_id,
                        [],
                    ).append(last_dialogue_id)
                    output_end_map.setdefault(segment_id, {}).setdefault(
                        first_dialogue_id,
                        {},
                    )[last_dialogue_id] = round(output_end_sec, 6)

        filtered[str(slot_id)] = {
            **{
                key: value
                for key, value in constraint.items()
                if key != "allowed_passages"
            },
            "allowed_segment_ids": allowed_segment_ids,
            "allowed_last_dialogue_ids_by_segment_and_first": endpoint_map,
            "output_audio_end_sec_by_segment_and_first_and_last": (
                output_end_map
            ),
        }
    return filtered, exclusions


def _validate_anchor_sequence(
    anchors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Require the model's complete Anchor selection to be one legal sequence."""

    for previous, current in zip(anchors, anchors[1:]):
        previous_source = parse_range(str(previous["source_window"]))
        current_source = parse_range(str(current["source_window"]))
        if set(previous["dialogue_ids"]) & set(current["dialogue_ids"]):
            raise ValueError("Dialogue anchors must not reuse dialogue items")
        if _audio_ranges_overlap(previous, current):
            raise _audio_overlap_error(previous, current)
        if (
            current_source[0] <= previous_source[0] + _TOLERANCE_SEC
            or current_source[0] < previous_source[1] - _TOLERANCE_SEC
        ):
            raise ValueError(
                "Anchor picture windows must be strictly increasing and "
                "non-overlapping in Slot order"
            )
    return anchors


def _select_non_overlapping_anchor_subset(
    anchors: list[dict[str, Any]],
    *,
    required_slot_ids: set[str],
    slots: list[dict[str, Any]],
    video_description: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep immutable Anchors and choose the strongest fully legal subset."""

    anchors_by_slot = {str(anchor["slot_id"]): anchor for anchor in anchors}
    missing_required = required_slot_ids - anchors_by_slot.keys()
    if missing_required:
        raise ValueError(
            "Required preserved dialogue Anchors are missing: "
            + ", ".join(sorted(missing_required))
        )
    required = [
        anchor
        for anchor in anchors
        if str(anchor["slot_id"]) in required_slot_ids
    ]

    def ordered_selection(
        selectable_subset: tuple[dict[str, Any], ...],
    ) -> list[dict[str, Any]]:
        selected_slot_ids = {
            str(anchor["slot_id"])
            for anchor in [*required, *selectable_subset]
        }
        return [
            anchor
            for anchor in anchors
            if str(anchor["slot_id"]) in selected_slot_ids
        ]

    def validate_subset(
        selectable_subset: tuple[dict[str, Any], ...],
    ) -> list[dict[str, Any]]:
        selected = ordered_selection(selectable_subset)
        _validate_anchor_sequence(selected)
        _build_partition_layout(
            slots,
            {
                str(anchor["slot_id"]): _time_range_ms(
                    anchor["source_window"]
                )
                for anchor in selected
            },
            video_description,
        )
        return selected

    validate_subset(())

    selectable = [
        anchor
        for anchor in anchors
        if str(anchor["slot_id"]) not in required_slot_ids
        and not any(_audio_ranges_overlap(anchor, kept) for kept in required)
    ]
    best: tuple[dict[str, Any], ...] = ()
    best_key = (-1, -1, -10**18)
    for size in range(len(selectable) + 1):
        for subset in combinations(selectable, size):
            try:
                selected = validate_subset(subset)
            except ValueError:
                continue
            anchor_ranges_by_slot = {
                str(anchor["slot_id"]): _time_range_ms(
                    anchor["source_window"]
                )
                for anchor in selected
            }
            key = (
                sum(
                    int(anchor["importance_likert"])
                    + int(anchor["coherence_likert"])
                    for anchor in subset
                ),
                len(subset),
                -_anchor_partition_balance_error_ms(
                    slots,
                    anchor_ranges_by_slot,
                    video_description,
                ),
            )
            if key > best_key:
                best = subset
                best_key = key

    kept = validate_subset(best)
    kept_slot_ids = {str(anchor["slot_id"]) for anchor in kept}
    rejected: list[dict[str, Any]] = []
    for anchor in anchors:
        slot_id = str(anchor["slot_id"])
        if slot_id in kept_slot_ids:
            continue
        start_sec, end_sec = _output_audio_range(anchor)
        conflicts = []
        for selected in kept:
            if not _audio_ranges_overlap(anchor, selected):
                continue
            selected_start, selected_end = _output_audio_range(selected)
            conflicts.append(
                {
                    "slot_id": str(selected["slot_id"]),
                    "output_audio_start_sec": round(selected_start, 6),
                    "output_audio_end_sec": round(selected_end, 6),
                }
            )
        if conflicts:
            rejected.append(
                {
                    "reason_code": "anchor_audio_overlap_filtered",
                    "slot_id": slot_id,
                    "output_audio_start_sec": round(start_sec, 6),
                    "output_audio_end_sec": round(end_sec, 6),
                    "conflicts_with_kept_anchors": conflicts,
                    "diagnosis": (
                        f"Proposed Anchor {slot_id} was excluded from the final "
                        "Anchor subset because its output audio overlaps a kept "
                        "Anchor."
                    ),
                }
            )
            continue

        trial_subset = tuple(
            candidate
            for candidate in anchors
            if str(candidate["slot_id"]) not in required_slot_ids
            and (
                str(candidate["slot_id"]) in kept_slot_ids
                or str(candidate["slot_id"]) == slot_id
            )
        )
        try:
            validate_subset(trial_subset)
        except ValueError as exc:
            combination_failure = str(exc)
        else:
            combination_failure = (
                "The proposal was incompatible with a stronger legal Anchor subset."
            )
        rejected.append(
            {
                "reason_code": "anchor_combination_invalid_filtered",
                "slot_id": slot_id,
                "output_audio_start_sec": round(start_sec, 6),
                "output_audio_end_sec": round(end_sec, 6),
                "combination_failure": combination_failure,
                "diagnosis": (
                    f"Proposed Anchor {slot_id} was excluded because the combined "
                    "Anchor selection violates source order, dialogue reuse, or "
                    "picture-partition capacity."
                ),
            }
        )
    return kept, rejected


def _dialogues_by_segment(
    video_description: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for segment in video_description["segments"]:
        segment_id = str(segment["segment_id"])
        dialogues: dict[str, dict[str, Any]] = {}
        for shot in segment["shots"]:
            for occurrence in shot.get("dialogue") or []:
                dialogue_id = str(occurrence["dialogue_id"])
                item = dialogues.setdefault(
                    dialogue_id,
                    {
                        "dialogue_id": dialogue_id,
                        "speaker": str(occurrence.get("speaker") or "").strip(),
                        "text": str(occurrence.get("text") or "").strip(),
                        "start_sec": float(occurrence["time_range"]["start_sec"]),
                        "end_sec": float(occurrence["time_range"]["end_sec"]),
                        "shot_ids": [],
                    },
                )
                item["start_sec"] = min(
                    float(item["start_sec"]),
                    float(occurrence["time_range"]["start_sec"]),
                )
                item["end_sec"] = max(
                    float(item["end_sec"]),
                    float(occurrence["time_range"]["end_sec"]),
                )
                shot_id = str(shot["shot_id"])
                if shot_id not in item["shot_ids"]:
                    item["shot_ids"].append(shot_id)
        result[segment_id] = sorted(
            (
                dialogue
                for dialogue in dialogues.values()
                if dialogue["text"]
                and dialogue["end_sec"] > dialogue["start_sec"] + _TOLERANCE_SEC
            ),
            key=lambda item: (item["start_sec"], item["end_sec"]),
        )
    return result


def _eligible_source_segments(
    slots: list[dict[str, Any]],
    video_description: dict[str, Any],
    dialogues: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    eligible_ids = {
        _source_segment_id(slot)
        for slot in slots
        if dialogues.get(_source_segment_id(slot))
    }
    return [
        {
            "segment_id": str(segment["segment_id"]),
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
            "dialogue_items": [
                {
                    "dialogue_id": dialogue["dialogue_id"],
                    "speaker": dialogue["speaker"],
                    "text": dialogue["text"],
                    "time_range": {
                        "start_sec": round(dialogue["start_sec"], 6),
                        "end_sec": round(dialogue["end_sec"], 6),
                    },
                    "duration_sec": round(
                        dialogue["end_sec"] - dialogue["start_sec"],
                        6,
                    ),
                    "source_shot_ids": dialogue["shot_ids"],
                }
                for dialogue in dialogues[str(segment["segment_id"])]
            ],
        }
        for segment in video_description["segments"]
        if str(segment["segment_id"]) in eligible_ids
    ]


def _dialogue_constraints_by_slot(
    slots: list[dict[str, Any]],
    video_description: dict[str, Any],
    dialogues: dict[str, list[dict[str, Any]]],
    min_anchor_duration_sec: float,
) -> dict[str, dict[str, Any]]:
    output_end = max(float(slot["output_end_sec"]) for slot in slots)
    segments = {
        str(segment["segment_id"]): segment
        for segment in video_description["segments"]
    }
    result: dict[str, dict[str, Any]] = {}
    group_slots_by_id = {
        group_id: group_slots
        for group_id, _segment_id, group_slots in _grouped_slots(slots)
    }
    for slot in slots:
        slot_id = str(slot["slot_id"])
        output_start = float(slot["output_start_sec"])
        maximum_audio_duration = output_end - output_start
        slot_duration_ms = _slot_duration_ms(slot)
        slot_duration = slot_duration_ms / 1000.0
        group_id = _base_group_id(slot)
        group_slots = group_slots_by_id[group_id]
        group_position = next(
            index
            for index, item in enumerate(group_slots)
            if str(item["slot_id"]) == slot_id
        )
        required_before_ms = sum(
            _slot_duration_ms(item) for item in group_slots[:group_position]
        )
        required_after_ms = sum(
            _slot_duration_ms(item) for item in group_slots[group_position + 1 :]
        )
        allowed_last_ids_by_first: dict[str, list[str]] = {}
        output_end_by_first_and_last: dict[str, dict[str, float]] = {}
        segment_id = _source_segment_id(slot)
        segment = segments[segment_id]
        segment_start_ms, segment_end_ms = _segment_bounds_ms(segment)
        sequence = dialogues.get(segment_id) or []
        for start_index, first in enumerate(sequence):
            speech_start = float(first["start_sec"])
            picture_start_ms = _seconds_to_ms(speech_start)
            picture_end_ms = picture_start_ms + slot_duration_ms
            if (
                picture_start_ms < segment_start_ms
                or picture_end_ms > segment_end_ms
                or picture_start_ms - segment_start_ms < required_before_ms
                or segment_end_ms - picture_end_ms < required_after_ms
            ):
                continue
            for end_index in range(start_index, len(sequence)):
                last = sequence[end_index]
                speech_end = float(last["end_sec"])
                speech_duration = speech_end - speech_start
                if speech_duration > maximum_audio_duration + _TOLERANCE_SEC:
                    continue
                if speech_duration >= min_anchor_duration_sec - _TOLERANCE_SEC:
                    allowed_last_ids_by_first.setdefault(
                        str(first["dialogue_id"]),
                        [],
                    ).append(
                        str(last["dialogue_id"])
                    )
                    output_end_by_first_and_last.setdefault(
                        str(first["dialogue_id"]),
                        {},
                    )[str(last["dialogue_id"])] = round(
                        output_start + speech_duration,
                        6,
                    )
        has_same_segment_sibling_slots = len(group_slots) > 1
        result[slot_id] = {
            "allowed_segment_ids": (
                [segment_id] if allowed_last_ids_by_first else []
            ),
            "allowed_last_dialogue_ids_by_segment_and_first": (
                {segment_id: allowed_last_ids_by_first}
                if allowed_last_ids_by_first
                else {}
            ),
            "output_audio_end_sec_by_segment_and_first_and_last": (
                {segment_id: output_end_by_first_and_last}
                if output_end_by_first_and_last
                else {}
            ),
            "group_id": group_id,
            "has_same_segment_sibling_slots": (
                has_same_segment_sibling_slots
            ),
            "preferred_anchor_picture_range": _preferred_anchor_picture_range(
                source_segment_id=segment_id,
                segment_start_ms=segment_start_ms,
                segment_end_ms=segment_end_ms,
                slot_index=group_position,
                slot_count=len(group_slots),
            ),
            "required_before_picture_ms": required_before_ms,
            "required_after_picture_ms": required_after_ms,
            "min_audio_duration_sec": round(min_anchor_duration_sec, 6),
            "max_audio_duration_sec": round(maximum_audio_duration, 6),
            "planned_picture_duration_sec": round(slot_duration, 6),
            "output_audio_start_sec": round(output_start, 6),
        }
    return result


def _warn_for_anchors_outside_preferred_picture_ranges(
    selections: list[dict[str, Any]],
    dialogue_constraints_by_slot: dict[str, dict[str, Any]],
    *,
    ignored_slot_ids: set[str],
) -> None:
    for selection in selections:
        slot_id = str(selection["slot_id"])
        if slot_id in ignored_slot_ids:
            continue
        guidance = dialogue_constraints_by_slot[slot_id][
            "preferred_anchor_picture_range"
        ]
        picture_start_ms, picture_end_ms = _time_range_ms(
            selection["source_window"]
        )
        preferred_start_ms = _seconds_to_ms(guidance["start_sec"])
        preferred_end_ms = _seconds_to_ms(guidance["end_sec"])
        if (
            picture_start_ms >= preferred_start_ms
            and picture_end_ms <= preferred_end_ms
        ):
            continue
        log_event(
            "WARNING",
            "aster.story",
            "stage.progress",
            "Dialogue Anchor is outside its preferred picture range; continuing",
            reason_code="anchor_outside_preferred_picture_range",
            slot_id=slot_id,
            group_id=str(
                dialogue_constraints_by_slot[slot_id]["group_id"]
            ),
            source_segment_id=str(selection["source_segment_id"]),
            actual_picture_range={
                "start_sec": round(picture_start_ms / 1000.0, 6),
                "end_sec": round(picture_end_ms / 1000.0, 6),
            },
            preferred_picture_range={
                "start_sec": round(preferred_start_ms / 1000.0, 6),
                "end_sec": round(preferred_end_ms / 1000.0, 6),
            },
            left_slot_count=int(guidance["left_slot_count"]),
            right_slot_count=int(guidance["right_slot_count"]),
        )


def _dialogue_item(
    dialogue: dict[str, Any],
) -> dict[str, Any]:
    return {
        "dialogue_id": dialogue["dialogue_id"],
        "speaker": dialogue["speaker"],
        "text": dialogue["text"],
        "time_range": {
            "start_sec": round(dialogue["start_sec"], 6),
            "end_sec": round(dialogue["end_sec"], 6),
        },
        "source_shot_ids": list(dialogue["shot_ids"]),
    }


def _validate_selection(
    parsed: dict[str, Any],
    slots: list[dict[str, Any]],
    video_description: dict[str, Any],
    dialogues: dict[str, list[dict[str, Any]]],
    dialogue_constraints_by_slot: dict[str, dict[str, Any]],
    anchor_config: DialogueAnchorConfig,
    *,
    require_anchor: bool = True,
    required_anchor_slot_ids: set[str] | None = None,
    overlap_rejections: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    slots_by_id = {
        str(slot["slot_id"]): slot
        for slot in slots
    }
    slot_order = {
        str(slot["slot_id"]): index
        for index, slot in enumerate(slots)
    }
    segments = {
        str(segment["segment_id"]): segment
        for segment in video_description["segments"]
    }
    output_start = min(float(slot["output_start_sec"]) for slot in slots)
    output_end = max(float(slot["output_end_sec"]) for slot in slots)
    selected: list[dict[str, Any]] = []
    raw_anchors = parsed.get("anchors") or []
    if require_anchor and not raw_anchors:
        raise ValueError(
            "At least one dialogue anchor is required when Anchor planning is enabled"
        )
    if len(raw_anchors) > anchor_config.max_anchors:
        raise ValueError(
            f"At most {anchor_config.max_anchors} dialogue anchors may be selected"
        )
    selected_slot_ids: set[str] = set()
    for raw in raw_anchors:
        slot_id = str(raw.get("slot_id") or "")
        if slot_id not in slots_by_id:
            raise ValueError(f"Unknown dialogue-anchor Slot: {slot_id}")
        if slot_id in selected_slot_ids:
            raise ValueError(
                f"Dialogue-anchor Slot {slot_id} may be selected at most once"
            )
        selected_slot_ids.add(slot_id)
        slot = slots_by_id[slot_id]
        constraint = dialogue_constraints_by_slot.get(slot_id) or {}
        segment_id = str(raw.get("source_segment_id") or "")
        allowed_segment_ids = {
            str(value)
            for value in constraint.get("allowed_segment_ids") or []
        }
        if segment_id not in allowed_segment_ids:
            raise ValueError(
                f"Dialogue anchor for {slot_id} uses disallowed Segment: "
                f"{segment_id}"
            )
        start_dialogue_id = str(raw.get("first_dialogue_id") or "")
        end_dialogue_id = str(raw.get("last_dialogue_id") or "")
        sequence = dialogues.get(segment_id) or []
        dialogue_positions = {
            dialogue["dialogue_id"]: index
            for index, dialogue in enumerate(sequence)
        }
        if (
            start_dialogue_id not in dialogue_positions
            or end_dialogue_id not in dialogue_positions
        ):
            raise ValueError(
                f"Dialogue range for {slot_id} must belong to {segment_id}"
            )
        start_index = dialogue_positions[start_dialogue_id]
        end_index = dialogue_positions[end_dialogue_id]
        if start_index > end_index:
            raise ValueError(
                f"Dialogue range for {slot_id} must follow source order"
            )
        selected_dialogues = sequence[start_index : end_index + 1]
        speech_start = float(selected_dialogues[0]["start_sec"])
        speech_end = float(selected_dialogues[-1]["end_sec"])
        speech_duration = speech_end - speech_start
        slot_duration_ms = _slot_duration_ms(slot)
        slot_duration = slot_duration_ms / 1000.0
        minimum_audio_duration = float(
            constraint["min_audio_duration_sec"]
        )
        maximum_audio_duration = float(
            constraint["max_audio_duration_sec"]
        )
        if speech_duration < minimum_audio_duration - _TOLERANCE_SEC:
            raise ValueError(
                f"Dialogue range for {slot_id} must last at least "
                f"{minimum_audio_duration:.3f} seconds"
            )
        if speech_duration > maximum_audio_duration + _TOLERANCE_SEC:
            raise ValueError(
                f"Dialogue range for {slot_id} exceeds its "
                f"{maximum_audio_duration:.3f}-second audio limit"
            )
        dialogue_output_start = float(slot["output_start_sec"])
        dialogue_output_end = dialogue_output_start + speech_duration
        if (
            dialogue_output_start < output_start - _TOLERANCE_SEC
            or dialogue_output_end > output_end + _TOLERANCE_SEC
        ):
            raise ValueError(
                f"Dialogue range for {slot_id} exceeds the output timeline "
                "when used as a start-aligned L-cut"
            )
        window_start = speech_start
        window_start_ms = _seconds_to_ms(window_start)
        window_end_ms = window_start_ms + slot_duration_ms
        window_start = window_start_ms / 1000.0
        window_end = window_end_ms / 1000.0
        segment = segments[segment_id]
        segment_start = float(segment["time_range"]["start_sec"])
        segment_end = float(segment["time_range"]["end_sec"])
        if (
            window_start < segment_start - _TOLERANCE_SEC
            or window_end > segment_end + _TOLERANCE_SEC
        ):
            raise ValueError(
                f"Dialogue placement for {slot_id} exceeds Segment bounds"
            )
        allowed_ranges = constraint.get(
            "allowed_last_dialogue_ids_by_segment_and_first"
        ) or {}
        allowed_last_ids = (
            allowed_ranges.get(segment_id, {}).get(start_dialogue_id, [])
            if isinstance(allowed_ranges, dict)
            else []
        )
        if end_dialogue_id not in {str(value) for value in allowed_last_ids}:
            raise ValueError(
                f"Dialogue range for {slot_id} is not an allowed endpoint pair"
            )
        source_range = (window_start, window_end)
        source_shot_ids = [
            str(shot["shot_id"])
            for shot in segment["shots"]
            if _overlaps(
                source_range,
                (
                    float(shot["time_range"]["start_sec"]),
                    float(shot["time_range"]["end_sec"]),
                ),
            )
        ]
        dialogue_items = [
            _dialogue_item(dialogue)
            for dialogue in selected_dialogues
        ]
        selected.append(
            {
                "anchor_id": (
                    start_dialogue_id
                    if start_dialogue_id == end_dialogue_id
                    else f"{start_dialogue_id}-{end_dialogue_id}"
                ),
                "slot_id": slot_id,
                "source_segment_id": segment_id,
                "source_shot_ids": source_shot_ids,
                "source_window": format_range(window_start, window_end),
                "dialogue_ids": [
                    dialogue["dialogue_id"]
                    for dialogue in selected_dialogues
                ],
                "dialogue_items": dialogue_items,
                "speakers": list(
                    dict.fromkeys(
                        dialogue["speaker"]
                        for dialogue in selected_dialogues
                        if dialogue["speaker"]
                    )
                ),
                "text": " ".join(
                    dialogue["text"]
                    for dialogue in selected_dialogues
                ),
                "transcript": "\n".join(
                    f"{dialogue['speaker']}: {dialogue['text']}"
                    if dialogue["speaker"]
                    else dialogue["text"]
                    for dialogue in selected_dialogues
                ),
                "dialogue_start_sec": round(speech_start, 6),
                "dialogue_end_sec": round(speech_end, 6),
                "output_audio_start_sec": round(dialogue_output_start, 6),
                "output_audio_end_sec": round(dialogue_output_end, 6),
                "audio_cut_style": "l_cut",
                "placement": "start",
                "narrative_significance": str(
                    raw.get("narrative_significance") or ""
                ).strip(),
                "request_relevance": str(
                    raw.get("request_relevance") or ""
                ).strip(),
                "standalone_meaning": str(
                    raw.get("standalone_meaning") or ""
                ).strip(),
                "importance_likert": int(raw["importance_likert"]),
                "coherence_likert": int(raw["coherence_likert"]),
            }
        )
    selected.sort(key=lambda item: slot_order[item["slot_id"]])
    selected, rejected_overlaps = _select_non_overlapping_anchor_subset(
        selected,
        required_slot_ids=set(required_anchor_slot_ids or set()),
        slots=slots,
        video_description=video_description,
    )
    if overlap_rejections is not None:
        overlap_rejections.extend(rejected_overlaps)
    if require_anchor and not selected:
        raise ValueError("No legal dialogue Anchor subset remains")
    _validate_anchor_sequence(selected)
    _build_partition_layout(
        slots,
        {
            str(anchor["slot_id"]): _time_range_ms(anchor["source_window"])
            for anchor in selected
        },
        video_description,
    )
    return selected


def _fixed_candidate(
    slot: dict[str, Any],
    selection: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    output_start = float(slot["output_start_sec"])
    output_end = float(slot["output_end_sec"])
    anchor = {
        "anchor_id": selection["anchor_id"],
        "dialogue_ids": selection["dialogue_ids"],
        "dialogue_items": selection["dialogue_items"],
        "speakers": selection["speakers"],
        "text": selection["text"],
        "transcript": selection["transcript"],
        "placement": selection["placement"],
        "source_video_timestamp": selection["source_window"],
        "source_shot_ids": selection["source_shot_ids"],
        "source_audio_start_sec": selection["dialogue_start_sec"],
        "source_audio_end_sec": selection["dialogue_end_sec"],
        "output_audio_start_sec": selection["output_audio_start_sec"],
        "output_audio_end_sec": selection["output_audio_end_sec"],
        "audio_cut_style": selection["audio_cut_style"],
        "audio_overlap_before_sec": round(
            max(
                0.0,
                output_start - float(selection["output_audio_start_sec"]),
            ),
            6,
        ),
        "audio_overlap_after_sec": round(
            max(
                0.0,
                float(selection["output_audio_end_sec"]) - output_end,
            ),
            6,
        ),
        "source_segment_id": selection["source_segment_id"],
        "narrative_significance": selection["narrative_significance"],
        "request_relevance": selection["request_relevance"],
        "standalone_meaning": selection["standalone_meaning"],
        "importance_likert": selection["importance_likert"],
        "coherence_likert": selection["coherence_likert"],
    }
    candidate = {
        "candidate_id": f"{slot['slot_id']}_dialogue_anchor",
        "slot_id": slot["slot_id"],
        "timestamp": selection["source_window"],
        "source_segment_id": selection["source_segment_id"],
        "source_shot_ids": selection["source_shot_ids"],
        "structured_context": (
            "Original contiguous dialogue anchor:\n"
            + selection["transcript"]
        ),
        "description": (
            "Source video synchronized to an original contiguous dialogue range."
        ),
        "matched_dialogue": selection["transcript"],
        "semantic_relevance": 0.8,
        "emotional_intensity": float(slot["target_emotional_intensity"]),
        "salience": 1.0,
        "visible_subjects": list(slot.get("required_visible_subjects") or []),
        "protagonist_visibility_likert": 3,
        "visual_slot_relevance_likert": 4,
        "visual_evidence": (
            "The source window preserves the selected original-picture and "
            "original-sound time mapping; identity was not assigned an artificial "
            "maximum visual score."
        ),
        "kinetic_energy": float(slot["target_kinetic_energy"]),
        "dialogue_anchor": anchor,
    }
    return anchor, candidate


def _apply_planning_partitions(
    slots: list[dict[str, Any]],
    video_description: dict[str, Any],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    anchor_ranges_by_slot: dict[str, tuple[int, int]] = {}
    for slot in slots:
        anchor = slot.get("dialogue_anchor")
        if anchor is None:
            continue
        anchor_ranges_by_slot[str(slot["slot_id"])] = _time_range_ms(
            anchor["source_video_timestamp"]
        )

    layout = _build_partition_layout(
        slots,
        anchor_ranges_by_slot,
        video_description,
    )
    updated_by_slot_id = {
        str(slot["slot_id"]): dict(slot)
        for slot in slots
    }
    planning_segments: list[dict[str, Any]] = []
    planning_groups: list[dict[str, Any]] = []
    for parent in layout:
        parent_group_id = str(parent["parent_group_id"])
        source_segment_id = str(parent["source_segment_id"])
        for anchor_index, anchor_layout in enumerate(parent["anchors"], 1):
            slot = updated_by_slot_id[str(anchor_layout["slot_id"])]
            slot["parent_group_id"] = parent_group_id
            slot["group_id"] = f"{parent_group_id}_anchor_{anchor_index:02d}"
            slot["source_segment_id"] = source_segment_id
            slot.pop("planning_segment_id", None)
        for child_group in parent["child_groups"]:
            planning_segment_id = str(child_group["planning_segment_id"])
            planning_segments.append(
                {
                    "planning_segment_id": planning_segment_id,
                    "source_segment_id": source_segment_id,
                    "start_ms": int(child_group["start_ms"]),
                    "end_ms": int(child_group["end_ms"]),
                }
            )
            group_artifact = {
                "group_id": str(child_group["group_id"]),
                "parent_group_id": parent_group_id,
                "source_segment_id": source_segment_id,
                "planning_segment_id": planning_segment_id,
                "slot_ids": list(child_group["slot_ids"]),
            }
            planning_groups.append(group_artifact)
            for slot_id in group_artifact["slot_ids"]:
                slot = updated_by_slot_id[str(slot_id)]
                slot["group_id"] = group_artifact["group_id"]
                slot["parent_group_id"] = parent_group_id
                slot["source_segment_id"] = source_segment_id
                slot["planning_segment_id"] = planning_segment_id

    return (
        [updated_by_slot_id[str(slot["slot_id"])] for slot in slots],
        planning_segments,
        planning_groups,
    )


def _raw_anchor_selection(slot: dict[str, Any]) -> dict[str, Any] | None:
    anchor = slot.get("dialogue_anchor")
    if not isinstance(anchor, dict):
        return None
    dialogue_ids = [str(value) for value in anchor.get("dialogue_ids") or []]
    if not dialogue_ids:
        raise ValueError(
            f"Preserved Anchor for {slot.get('slot_id')} has no dialogue_ids"
        )
    return {
        "slot_id": str(slot["slot_id"]),
        "source_segment_id": str(anchor["source_segment_id"]),
        "first_dialogue_id": dialogue_ids[0],
        "last_dialogue_id": dialogue_ids[-1],
        "narrative_significance": str(anchor["narrative_significance"]),
        "request_relevance": str(anchor["request_relevance"]),
        "standalone_meaning": str(anchor["standalone_meaning"]),
        "importance_likert": int(anchor["importance_likert"]),
        "coherence_likert": int(anchor["coherence_likert"]),
    }


def select_dialogue_anchors(
    slots: list[dict[str, Any]],
    config: LLMConfig,
    anchor_config: DialogueAnchorConfig,
    context: WorkflowContext,
    *,
    selectable_slot_ids: set[str] | None = None,
    preserved_selections: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    slots = _normalise_arrangement_slots(slots)
    video_description = context.get_artifact("video_description")
    if video_description is None:
        raise RuntimeError(
            "Video description is required for dialogue anchor selection"
        )
    dialogues = _dialogues_by_segment(video_description)
    dialogue_constraints_by_slot = _dialogue_constraints_by_slot(
        slots,
        video_description,
        dialogues,
        anchor_config.min_anchor_duration_sec,
    )
    slot_ids = {str(slot["slot_id"]) for slot in slots}
    selected_slot_ids = (
        set(slot_ids) if selectable_slot_ids is None else set(selectable_slot_ids)
    )
    unknown_slot_ids = selected_slot_ids - slot_ids
    if unknown_slot_ids:
        raise ValueError(
            "Unknown selectable dialogue-anchor Slots: "
            + ", ".join(sorted(unknown_slot_ids))
        )
    preserved = [dict(item) for item in preserved_selections or []]
    preserved_slot_ids = {str(item.get("slot_id") or "") for item in preserved}
    overlap = selected_slot_ids & preserved_slot_ids
    if overlap:
        raise ValueError(
            "Preserved and selectable dialogue-anchor Slots overlap: "
            + ", ".join(sorted(overlap))
        )
    normalized_preserved = (
        _validate_selection(
            {"anchors": preserved},
            slots,
            video_description,
            dialogues,
            dialogue_constraints_by_slot,
            anchor_config,
            require_anchor=False,
            required_anchor_slot_ids=preserved_slot_ids,
        )
        if preserved
        else []
    )
    remaining_anchor_count = anchor_config.max_anchors - len(preserved)
    if remaining_anchor_count < 0:
        raise ValueError(
            f"At most {anchor_config.max_anchors} dialogue anchors may be selected"
        )
    selectable_slots = [
        slot for slot in slots if str(slot["slot_id"]) in selected_slot_ids
    ]
    selectable_constraints = {
        slot_id: dialogue_constraints_by_slot[slot_id]
        for slot_id in selected_slot_ids
    }
    selectable_constraints, preserved_conflict_exclusions = (
        _filter_constraints_against_preserved(
            selectable_constraints,
            normalized_preserved,
        )
    )
    context.set_artifact(
        "anchor_preserved_conflict_exclusions",
        preserved_conflict_exclusions,
    )
    valid_segment_ids = {
        str(segment_id)
        for constraint in selectable_constraints.values()
        for segment_id in constraint["allowed_segment_ids"]
    }
    source_segments = _eligible_source_segments(
        selectable_slots,
        video_description,
        dialogues,
    )
    source_segments = [
        segment
        for segment in source_segments
        if str(segment["segment_id"]) in valid_segment_ids
    ]
    should_request = bool(
        remaining_anchor_count > 0
        and source_segments
        and any(
            constraint["allowed_segment_ids"]
            for constraint in selectable_constraints.values()
        )
    )
    overlap_rejections: list[dict[str, Any]] = []

    def validate_combined(parsed: dict[str, Any]) -> list[dict[str, Any]]:
        overlap_rejections.clear()
        new_selections = list(parsed.get("anchors") or [])
        return _validate_selection(
            {"anchors": [*preserved, *new_selections]},
            slots,
            video_description,
            dialogues,
            dialogue_constraints_by_slot,
            anchor_config,
            required_anchor_slot_ids=preserved_slot_ids,
            overlap_rejections=overlap_rejections,
        )

    if should_request:
        video_summary = context.get_artifact("video_summary")
        if video_summary is None:
            raise RuntimeError(
                "Video summary is required for dialogue anchor selection"
            )
        package = prompt_registry.build(
            PromptStage.PLANNERS,
            PromptTask.DIALOGUE_ANCHOR_SELECTION,
            DialogueAnchorSelectionDetails(
                slots=selectable_slots,
                video_summary=video_summary,
                source_segments=source_segments,
                dialogue_constraints_by_slot=selectable_constraints,
                max_anchors=remaining_anchor_count,
                min_anchor_duration_sec=anchor_config.min_anchor_duration_sec,
                min_anchors=0 if preserved else 1,
                preserved_anchors=tuple(
                    {
                        "slot_id": str(item["slot_id"]),
                        "source_segment_id": str(item["source_segment_id"]),
                        "first_dialogue_id": str(item["dialogue_ids"][0]),
                        "last_dialogue_id": str(item["dialogue_ids"][-1]),
                        "output_audio_start_sec": float(
                            item["output_audio_start_sec"]
                        ),
                        "output_audio_end_sec": float(
                            item["output_audio_end_sec"]
                        ),
                    }
                    for item in normalized_preserved
                ),
            ),
        )
        selected = context.call_prompt(
            package=package,
            config=config,
            validate_business=validate_combined,
        )
    else:
        if not preserved:
            if not source_segments:
                raise ValueError(
                    "No eligible dialogue Segment is assigned to the current Arrangement"
                )
            raise ValueError(
                "No eligible dialogue passage can satisfy the current Arrangement capacity"
            )
        selected = validate_combined({"anchors": []})
    context.set_artifact(
        "anchor_overlap_rejections",
        list(overlap_rejections),
    )
    for rejection in overlap_rejections:
        log_event(
            "WARNING",
            "aster.story",
            "validation.reject",
            "Excluded overlapping dialogue Anchor proposal",
            **rejection,
        )
    _warn_for_anchors_outside_preferred_picture_ranges(
        selected,
        dialogue_constraints_by_slot,
        ignored_slot_ids=preserved_slot_ids,
    )
    selected_by_slot = {
        item["slot_id"]: item
        for item in selected
    }
    result: list[dict[str, Any]] = []
    anchors: list[dict[str, Any]] = []
    for slot in slots:
        item = dict(slot)
        selection = selected_by_slot.get(slot["slot_id"])
        if selection is not None:
            anchor, candidate = _fixed_candidate(item, selection)
            item["dialogue_anchor"] = anchor
            item["fixed_candidate"] = candidate
            anchors.append({"slot_id": item["slot_id"], **anchor})
        result.append(item)
    result, planning_segments, planning_groups = _apply_planning_partitions(
        result,
        video_description,
    )
    context.set_artifact("planning_segments", planning_segments)
    context.set_artifact("planning_groups", planning_groups)
    context.set_artifact("dialogue_anchors", anchors)
    return result


class StoryEditorAgent:
    """S agent: anchor the requested story with selected original dialogue."""

    def __init__(self, config: AppConfig, context: WorkflowContext) -> None:
        self.config = config
        self.context = context

    def anchor(
        self,
        slots: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        model_config = replace(
            self.config.llm,
            max_retries=(
                self.config.planners.dialogue_anchors.max_model_requests - 1
            ),
        )
        return select_dialogue_anchors(
            slots,
            model_config,
            self.config.planners.dialogue_anchors,
            self.context,
        )

    def anchor_groups(
        self,
        slots: list[dict[str, Any]],
        *,
        previous_slots: list[dict[str, Any]],
        replanned_slot_ids: set[str],
    ) -> list[dict[str, Any]]:
        """Re-run Story only for changed complete Arrangement groups."""

        current = _normalise_arrangement_slots(slots)
        previous_arrangement = _normalise_arrangement_slots(previous_slots)
        current_slot_ids = {str(slot["slot_id"]) for slot in current}
        missing = set(replanned_slot_ids) - current_slot_ids
        if missing:
            raise ValueError(
                "Replanned Slots are missing from the repaired Arrangement: "
                + ", ".join(sorted(missing))
            )
        previous_groups = {
            group_id: group_slots
            for group_id, _segment_id, group_slots in _grouped_slots(
                previous_arrangement
            )
        }
        reusable_group_ids: set[str] = set()
        selectable_slot_ids: set[str] = set()
        for group_id, _segment_id, group_slots in _grouped_slots(current):
            slot_ids = {str(slot["slot_id"]) for slot in group_slots}
            if (
                not (slot_ids & replanned_slot_ids)
                and previous_groups.get(group_id) is not None
                and _same_arrangement_group(
                    previous_groups[group_id],
                    group_slots,
                )
            ):
                reusable_group_ids.add(group_id)
            else:
                selectable_slot_ids.update(slot_ids)

        preserved_selections = [
            raw
            for slot in previous_slots
            if _base_group_id(slot) in reusable_group_ids
            if (raw := _raw_anchor_selection(slot)) is not None
        ]
        model_config = replace(
            self.config.llm,
            max_retries=(
                self.config.planners.dialogue_anchors.max_model_requests - 1
            ),
        )
        return select_dialogue_anchors(
            current,
            model_config,
            self.config.planners.dialogue_anchors,
            self.context,
            selectable_slot_ids=selectable_slot_ids,
            preserved_selections=preserved_selections,
        )

    def restore_arrangement_slots(
        self,
        slots: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Remove Story partitions before an Arrangement group is repaired."""

        return _normalise_arrangement_slots(slots)


__all__ = ["StoryEditorAgent"]
