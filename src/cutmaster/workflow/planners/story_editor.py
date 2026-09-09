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
        segment_id = _source_segment_id(slot)
        segment = segments[segment_id]
        segment_start_ms, segment_end_ms = _segment_bounds_ms(segment)
        sequence = dialogues.get(segment_id) or []
        has_feasible_passage = False
        for start_index, first in enumerate(sequence):
            speech_start = float(first["start_sec"])
            picture_start_ms = _seconds_to_ms(speech_start)
            picture_end_ms = picture_start_ms + slot_duration_ms
            if (
                picture_start_ms < segment_start_ms
                or picture_end_ms > segment_end_ms
            ):
                continue
            for end_index in range(start_index, len(sequence)):
                last = sequence[end_index]
                speech_end = float(last["end_sec"])
                speech_duration = speech_end - speech_start
                if speech_duration > maximum_audio_duration + _TOLERANCE_SEC:
                    break
                if speech_duration >= min_anchor_duration_sec - _TOLERANCE_SEC:
                    has_feasible_passage = True
                    break
            if has_feasible_passage:
                break
        has_same_segment_sibling_slots = len(group_slots) > 1
        result[slot_id] = {
            "allowed_segment_ids": [segment_id] if has_feasible_passage else [],
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
    required_anchor_slot_ids: set[str] | None = None,
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
    if len(raw_anchors) > anchor_config.max_anchors:
        raise ValueError(
            f"At most {anchor_config.max_anchors} dialogue anchors may be selected"
        )
    for raw in raw_anchors:
        slot_id = str(raw.get("slot_id") or "")
        if slot_id not in slots_by_id:
            raise ValueError(f"Unknown dialogue-anchor Slot: {slot_id}")
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
    required = set(required_anchor_slot_ids or set())

    def legal_subset(values: tuple[dict[str, Any], ...]) -> bool:
        slot_ids = [str(anchor["slot_id"]) for anchor in values]
        if len(slot_ids) != len(set(slot_ids)) or not required.issubset(slot_ids):
            return False
        for previous, current in zip(values, values[1:]):
            previous_source = parse_range(str(previous["source_window"]))
            current_source = parse_range(str(current["source_window"]))
            if set(previous["dialogue_ids"]) & set(current["dialogue_ids"]):
                return False
            if _audio_ranges_overlap(previous, current):
                return False
            if (
                current_source[0] <= previous_source[0] + _TOLERANCE_SEC
                or current_source[0] < previous_source[1] - _TOLERANCE_SEC
            ):
                return False
        try:
            _build_partition_layout(
                slots,
                {
                    str(anchor["slot_id"]): _time_range_ms(
                        anchor["source_window"]
                    )
                    for anchor in values
                },
                video_description,
            )
        except ValueError:
            return False
        return True

    def subset_score(values: tuple[dict[str, Any], ...]) -> tuple[float, int, int]:
        return (
            round(
                sum(
                    float(anchor["output_audio_end_sec"])
                    - float(anchor["output_audio_start_sec"])
                    for anchor in values
                ),
                6,
            ),
            sum(int(anchor["importance_likert"]) for anchor in values),
            sum(int(anchor["coherence_likert"]) for anchor in values),
        )

    for subset_size in range(len(selected), -1, -1):
        legal = [
            subset
            for subset in combinations(selected, subset_size)
            if legal_subset(subset)
        ]
        if legal:
            return list(max(legal, key=subset_score))
    if required:
        raise ValueError(
            "Repair invalidated a preserved dialogue Anchor: "
            + ", ".join(sorted(required))
        )
    return []


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


def _apply_anchor_selections(
    slots: list[dict[str, Any]],
    selections: list[dict[str, Any]],
    video_description: dict[str, Any],
    context: WorkflowContext,
) -> list[dict[str, Any]]:
    selected_by_slot = {
        str(item["slot_id"]): item for item in selections
    }
    result: list[dict[str, Any]] = []
    anchors: list[dict[str, Any]] = []
    for slot in slots:
        item = dict(slot)
        selection = selected_by_slot.get(str(slot["slot_id"]))
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


def select_dialogue_anchors(
    slots: list[dict[str, Any]],
    config: LLMConfig,
    anchor_config: DialogueAnchorConfig,
    context: WorkflowContext,
) -> list[dict[str, Any]]:
    slots = _normalise_arrangement_slots(slots)
    video_description = context.get_artifact("video_description")
    if video_description is None:
        raise RuntimeError(
            "Video description is required for dialogue anchor selection"
        )
    if not anchor_config.enabled:
        return _apply_anchor_selections(slots, [], video_description, context)
    dialogues = _dialogues_by_segment(video_description)
    dialogue_constraints_by_slot = _dialogue_constraints_by_slot(
        slots,
        video_description,
        dialogues,
        anchor_config.min_anchor_duration_sec,
    )
    valid_segment_ids = {
        str(segment_id)
        for constraint in dialogue_constraints_by_slot.values()
        for segment_id in constraint["allowed_segment_ids"]
    }
    source_segments = _eligible_source_segments(
        slots,
        video_description,
        dialogues,
    )
    source_segments = [
        segment
        for segment in source_segments
        if str(segment["segment_id"]) in valid_segment_ids
    ]
    if source_segments:
        video_summary = context.get_artifact("video_summary")
        if video_summary is None:
            raise RuntimeError(
                "Video summary is required for dialogue anchor selection"
            )
        package = prompt_registry.build(
            PromptStage.PLANNERS,
            PromptTask.DIALOGUE_ANCHOR_SELECTION,
            DialogueAnchorSelectionDetails(
                slots=slots,
                video_summary=video_summary,
                source_segments=source_segments,
                dialogue_constraints_by_slot=dialogue_constraints_by_slot,
                max_anchors=anchor_config.max_anchors,
                min_anchor_duration_sec=anchor_config.min_anchor_duration_sec,
            ),
        )
        selected = context.call_prompt(
            package=package,
            config=config,
            validate_business=lambda parsed: _validate_selection(
                parsed,
                slots,
                video_description,
                dialogues,
                dialogue_constraints_by_slot,
                anchor_config,
            ),
        )
    else:
        selected = []
    _warn_for_anchors_outside_preferred_picture_ranges(
        selected,
        dialogue_constraints_by_slot,
        ignored_slot_ids=set(),
    )
    return _apply_anchor_selections(
        slots,
        selected,
        video_description,
        context,
    )


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
        """Preserve legal Anchors and rebuild Story partitions without a model call."""

        current = _normalise_arrangement_slots(slots)
        current_slot_ids = {str(slot["slot_id"]) for slot in current}
        missing = set(replanned_slot_ids) - current_slot_ids
        if missing:
            raise ValueError(
                "Replanned Slots are missing from the repaired Arrangement: "
                + ", ".join(sorted(missing))
            )
        preserved_selections = [
            raw
            for slot in previous_slots
            if (raw := _raw_anchor_selection(slot)) is not None
        ]
        video_description = self.context.get_artifact("video_description")
        if video_description is None:
            raise RuntimeError(
                "Video description is required for dialogue anchor preservation"
            )
        dialogues = _dialogues_by_segment(video_description)
        constraints = _dialogue_constraints_by_slot(
            current,
            video_description,
            dialogues,
            self.config.planners.dialogue_anchors.min_anchor_duration_sec,
        )
        required_slot_ids = {
            str(item["slot_id"]) for item in preserved_selections
        }
        try:
            selected = _validate_selection(
                {"anchors": preserved_selections},
                current,
                video_description,
                dialogues,
                constraints,
                self.config.planners.dialogue_anchors,
                required_anchor_slot_ids=required_slot_ids,
            )
        except ValueError as exc:
            raise ValueError(
                "Arrangement repair invalidated a preserved dialogue Anchor"
            ) from exc
        return _apply_anchor_selections(
            current,
            selected,
            video_description,
            self.context,
        )

    def restore_arrangement_slots(
        self,
        slots: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Remove Story partitions before an Arrangement group is repaired."""

        return _normalise_arrangement_slots(slots)


__all__ = ["StoryEditorAgent"]
