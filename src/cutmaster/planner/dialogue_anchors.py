from __future__ import annotations

from typing import Any

from cutmaster.configuration.schema import DialogueAnchorConfig, LLMConfig
from cutmaster.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.prompting.planner import DialogueAnchorSelectionDetails
from cutmaster.runtime.workflow_context import WorkflowContext
from cutmaster.timecode import format_range, parse_range


_TOLERANCE_SEC = 0.001


def _overlaps(
    first: tuple[float, float],
    second: tuple[float, float],
) -> bool:
    return first[0] < second[1] - _TOLERANCE_SEC and second[0] < first[1] - _TOLERANCE_SEC


def _optimal_non_overlapping_anchors(
    anchors: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Choose the chronological subset with most anchors, then most speech."""
    if not anchors:
        return []

    def compatible(
        previous: dict[str, Any],
        current: dict[str, Any],
    ) -> bool:
        previous_source = parse_range(str(previous["source_window"]))
        current_source = parse_range(str(current["source_window"]))
        return (
            str(previous["slot_id"]) != str(current["slot_id"])
            and set(previous["dialogue_ids"]).isdisjoint(current["dialogue_ids"])
            and float(previous["output_audio_end_sec"])
            <= float(current["output_audio_start_sec"]) + _TOLERANCE_SEC
            and previous_source[1] <= current_source[0] + _TOLERANCE_SEC
        )

    def score(path: list[dict[str, Any]]) -> tuple[int, float]:
        return (
            len(path),
            round(
                sum(
                    float(anchor["output_audio_end_sec"])
                    - float(anchor["output_audio_start_sec"])
                    for anchor in path
                ),
                6,
            ),
        )

    paths_ending_at: list[list[dict[str, Any]]] = []
    best: list[dict[str, Any]] = []
    for index, anchor in enumerate(anchors):
        best_ending_here = [anchor]
        for previous_index in range(index):
            previous_path = paths_ending_at[previous_index]
            if not compatible(previous_path[-1], anchor):
                continue
            candidate = [*previous_path, anchor]
            if score(candidate) > score(best_ending_here):
                best_ending_here = candidate
        paths_ending_at.append(best_ending_here)
        if score(best_ending_here) > score(best):
            best = best_ending_here
    return best


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
        str(segment_id)
        for slot in slots
        for segment_id in slot["source_segment_ids"]
        if dialogues.get(str(segment_id))
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
            "dialogue_context": segment["dialogue_context"],
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
    for slot in slots:
        slot_id = str(slot["slot_id"])
        output_start = float(slot["output_start_sec"])
        maximum_audio_duration = output_end - output_start
        slot_duration = float(slot["planned_duration_sec"])
        allowed_segment_ids: list[str] = []
        for segment_id_value in slot["source_segment_ids"]:
            segment_id = str(segment_id_value)
            segment = segments[segment_id]
            segment_end = float(segment["time_range"]["end_sec"])
            sequence = dialogues.get(segment_id) or []
            feasible = False
            for start_index, first in enumerate(sequence):
                speech_start = float(first["start_sec"])
                if speech_start + slot_duration > segment_end + _TOLERANCE_SEC:
                    continue
                for end_index in range(start_index, len(sequence)):
                    last = sequence[end_index]
                    speech_end = float(last["end_sec"])
                    speech_duration = speech_end - speech_start
                    if speech_duration > maximum_audio_duration + _TOLERANCE_SEC:
                        break
                    if speech_duration >= min_anchor_duration_sec - _TOLERANCE_SEC:
                        feasible = True
                        break
                if feasible:
                    break
            if feasible:
                allowed_segment_ids.append(segment_id)
        result[slot_id] = {
            "allowed_segment_ids": allowed_segment_ids,
            "min_audio_duration_sec": round(min_anchor_duration_sec, 6),
            "max_audio_duration_sec": round(maximum_audio_duration, 6),
            "planned_picture_duration_sec": round(slot_duration, 6),
            "output_audio_start_sec": round(output_start, 6),
        }
    return result


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
        slot_duration = float(slot["planned_duration_sec"])
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
        window_end = window_start + slot_duration
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
    return _optimal_non_overlapping_anchors(selected)


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
        "source_segment_ids": [selection["source_segment_id"]],
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


def select_dialogue_anchors(
    slots: list[dict[str, Any]],
    config: LLMConfig,
    anchor_config: DialogueAnchorConfig,
    context: WorkflowContext,
) -> list[dict[str, Any]]:
    slots = [
        {
            key: value
            for key, value in slot.items()
            if key not in {"dialogue_anchor", "fixed_candidate"}
        }
        for slot in slots
    ]
    video_description = context.get_artifact("video_description")
    if video_description is None:
        raise RuntimeError(
            "Video description is required for dialogue anchor selection"
        )
    dialogues = _dialogues_by_segment(video_description)
    source_segments = _eligible_source_segments(
        slots,
        video_description,
        dialogues,
    )
    if not source_segments:
        context.set_artifact("dialogue_anchors", [])
        return [dict(slot) for slot in slots]
    dialogue_constraints_by_slot = _dialogue_constraints_by_slot(
        slots,
        video_description,
        dialogues,
        anchor_config.min_anchor_duration_sec,
    )
    if not any(
        constraint["allowed_segment_ids"]
        for constraint in dialogue_constraints_by_slot.values()
    ):
        context.set_artifact("dialogue_anchors", [])
        return [dict(slot) for slot in slots]
    valid_segment_ids = {
        str(segment_id)
        for constraint in dialogue_constraints_by_slot.values()
        for segment_id in constraint["allowed_segment_ids"]
    }
    source_segments = [
        segment
        for segment in source_segments
        if str(segment["segment_id"]) in valid_segment_ids
    ]
    video_summary = context.get_artifact("video_summary")
    if video_summary is None:
        raise RuntimeError(
            "Video summary is required for dialogue anchor selection"
        )
    package = prompt_registry.build(
        PromptStage.PLANNER,
        PromptTask.DIALOGUE_ANCHOR_SELECTION,
        DialogueAnchorSelectionDetails(
            slots=slots,
            video_summary=video_summary,
            source_segments=source_segments,
            dialogue_constraints_by_slot=dialogue_constraints_by_slot,
            max_anchors=anchor_config.max_anchors,
            min_anchor_duration_sec=(
                anchor_config.min_anchor_duration_sec
            ),
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
    context.set_artifact("dialogue_anchors", anchors)
    return result


__all__ = ["select_dialogue_anchors"]
