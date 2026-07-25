from __future__ import annotations

import json
import math
from typing import Any

from cutmaster.models import LLMConfig, RunRequest
from cutmaster.planner_context import PlanningContext

PLANNER_SYSTEM = (
    "You are the planning component of a professional video editor. "
    "Plan an output timeline but do not select source timestamps. Return strict JSON only."
)

def _request_metadata(request: RunRequest, clip_count: int) -> dict[str, Any]:
    return {
        "instruction": request.prompt,
        "prompt_type": request.prompt_type,
        "video_title": request.video_title or request.video_path.stem,
        "target_duration_sec": request.target_output_length_sec,
        "target_shot_length_sec": request.target_shot_length_sec,
        "clip_count": clip_count,
    }


def _validate_slots(
    parsed: dict[str, Any],
    clip_count: int,
    video_description: dict[str, Any],
    forbidden_segment_ids: set[str],
    failed_segment_assignments: set[tuple[str, ...]],
) -> list[dict[str, Any]]:
    raw = parsed.get("slots")
    if not isinstance(raw, list) or len(raw) != clip_count:
        raise ValueError(f"Expected exactly {clip_count} edit slots")
    slots: list[dict[str, Any]] = []
    source_segments = video_description["segments"]
    segment_order = {
        str(segment["segment_id"]): index
        for index, segment in enumerate(source_segments)
    }
    previous_segment_index = -1
    for index, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Slot {index} must be an object")
        description = str(item.get("content_description") or "").strip()
        if not description:
            raise ValueError(f"Slot {index} has no content description")
        duration = float(item.get("desired_duration_sec") or 0)
        if duration <= 0:
            raise ValueError(f"Slot {index} has invalid desired duration")
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
        primary_segment_index = min(
            segment_order[segment_id] for segment_id in segment_ids
        )
        if primary_segment_index < previous_segment_index:
            raise ValueError("Slot source Segments must be in nondecreasing source order")
        previous_segment_index = primary_segment_index
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
    return slots


def plan_edit_slots(
    request: RunRequest,
    music_profile: dict[str, Any],
    config: LLMConfig,
    context: PlanningContext,
) -> list[dict[str, Any]]:
    clip_count = request.custom_clips or max(
        1, math.ceil(request.target_output_length_sec / request.target_shot_length_sec)
    )
    context.set_artifact("request", _request_metadata(request, clip_count))
    context.set_artifact("music_profile", music_profile)
    video_description = context.get_artifact("video_description")
    if video_description is None:
        raise RuntimeError("Video description must be available before edit-slot planning")
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
    prompt = f"""Create exactly {clip_count} sequential edit slots for the maintained request,
music profile, and structured video description.

Each slot must be realizable from one or more supplied source_segment_ids. Use the Shot-level VLM
descriptions, characters, scenes, dialogue, and Segment summaries as source truth. Never invent
props, gestures, settings, identities, or actions absent from the structured description.
Keep source_segment_ids in nondecreasing source order across slots. Reusing a Segment for adjacent
slots is allowed when it contains enough distinct Shots. For a character-focused request, list
the focal character in required_visible_subjects for every Slot where that character must be seen.
Use the music sections and energy curve to vary desired duration: high kinetic energy generally uses shorter clips; low energy uses longer clips.
Maintain a coherent progression. Every slot after the first must explain how it continues or contrasts with the previous slot.
The desired durations should total approximately {request.target_output_length_sec:.1f} seconds.
{retry_note}

Return:
{{"slots":[{{
  "narrative_role":"setup|development|turning_point|climax|resolution",
  "content_description":"people, visible action, emotion, setting, editorial purpose",
  "target_emotion":"short label",
  "target_emotional_intensity":0.0,
  "target_kinetic_energy":0.0,
  "desired_duration_sec":4.0,
  "continuity_from_previous":"semantic or visual relationship",
  "source_segment_ids":["segment_0001"],
  "required_visible_subjects":["focal character name"]
}}]}}"""
    return context.call_json(
        operation="Edit slot planning",
        prompt=prompt,
        config=config,
        context_keys=[
            "request",
            "music_profile",
            "video_description",
            "planning_feedback",
        ],
        system_prompt=PLANNER_SYSTEM,
        enable_thinking=True,
        validate=lambda parsed: _validate_slots(
            parsed,
            clip_count,
            video_description,
            forbidden_segment_ids,
            failed_segment_assignments,
        ),
        output_artifact="edit_plan_unaligned",
    )


def align_slots_to_music(
    slots: list[dict[str, Any]],
    music_profile: dict[str, Any],
    total_duration_sec: float,
    output_fps: int,
) -> list[dict[str, Any]]:
    weights = [max(0.1, float(slot["desired_duration_sec"])) for slot in slots]
    scale = total_duration_sec / sum(weights)
    elapsed = 0.0
    ideal: list[float] = []
    for weight in weights[:-1]:
        elapsed += weight * scale
        ideal.append(elapsed)
    accents = music_profile["accents_sec"]
    try:
        boundaries = _globally_align_boundaries(
            ideal, accents, total_duration_sec, output_fps
        )
    except ValueError:
        boundaries = _globally_align_boundaries(
            ideal, music_profile["beats_sec"], total_duration_sec, output_fps
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
        if value >= min_clip_duration_sec:
            states[index] = (abs(value - ideal[0]), [value])
    for boundary_index in range(1, len(ideal)):
        next_states: dict[int, tuple[float, list[float]]] = {}
        for index, value in enumerate(values):
            best: tuple[float, list[float]] | None = None
            for previous_index, (cost, path) in states.items():
                if previous_index >= index or value - path[-1] < min_clip_duration_sec:
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
    ]
    if not feasible:
        raise ValueError("No musical-boundary path leaves room for the final clip")
    return min(feasible, key=lambda state: state[0])[1]
