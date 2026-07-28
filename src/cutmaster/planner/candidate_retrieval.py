from __future__ import annotations

import json
import math
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import cv2

from cutmaster.configuration.schema import CandidateRetrievalConfig, LLMConfig, VLMConfig
from cutmaster.runtime.observability import error_summary, log_event
from cutmaster.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.prompting.planner import (
    CandidateRetrievalDetails,
    CandidateVisualScoringDetails,
)
from cutmaster.runtime.workflow_context import WorkflowContext
from cutmaster.planner.scoring import _contact_sheet_data_url, _normalize_likert_score
from cutmaster.planner.media import SegmentMediaReader
from cutmaster.runtime.progress import progress_bar, progress_iter
from cutmaster.timecode import format_range, parse_range


_TIMESTAMP_TOLERANCE_SEC = 0.0011
_VISUALLY_STATIC_MAX_KINETIC_ENERGY = 0.01


def _ranges_overlap(
    first: tuple[float, float],
    second: tuple[float, float],
) -> bool:
    return (
        first[0] < second[1] - _TIMESTAMP_TOLERANCE_SEC
        and second[0] < first[1] - _TIMESTAMP_TOLERANCE_SEC
    )


def _merge_ranges(
    ranges: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(ranges):
        if end <= start:
            continue
        if (
            not merged
            or start > merged[-1][1] + _TIMESTAMP_TOLERANCE_SEC
        ):
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _segment_ranges(segments: list[dict[str, Any]]) -> list[tuple[float, float]]:
    return _merge_ranges(
        [
            (
                float(segment["time_range"]["start_sec"]),
                float(segment["time_range"]["end_sec"]),
            )
            for segment in segments
        ]
    )


def _window_capacity(
    segments: list[dict[str, Any]],
    duration_sec: float,
    excluded_ranges: list[str],
) -> int:
    exclusions = _merge_ranges(
        [parse_range(timestamp) for timestamp in excluded_ranges]
    )
    capacity = 0
    for allowed_start, allowed_end in _segment_ranges(segments):
        cursor = allowed_start
        for excluded_start, excluded_end in exclusions:
            if excluded_end <= cursor + _TIMESTAMP_TOLERANCE_SEC:
                continue
            if excluded_start >= allowed_end - _TIMESTAMP_TOLERANCE_SEC:
                break
            free_end = min(excluded_start, allowed_end)
            capacity += math.floor(
                max(0.0, free_end - cursor + _TIMESTAMP_TOLERANCE_SEC)
                / duration_sec
            )
            cursor = max(cursor, excluded_end)
            if cursor >= allowed_end - _TIMESTAMP_TOLERANCE_SEC:
                break
        capacity += math.floor(
            max(0.0, allowed_end - cursor + _TIMESTAMP_TOLERANCE_SEC)
            / duration_sec
        )
    return capacity


def _validate_candidates(
    parsed: dict[str, Any],
    slots: list[dict[str, Any]],
    source_segments_by_slot: dict[str, list[dict[str, Any]]],
    per_slot: int,
    excluded_ranges_by_slot: dict[str, list[str]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    excluded_ranges_by_slot = excluded_ranges_by_slot or {}
    allowed_slots = {slot["slot_id"] for slot in slots}
    slots_by_id = {slot["slot_id"]: slot for slot in slots}
    result: dict[str, list[dict[str, Any]]] = {slot_id: [] for slot_id in allowed_slots}
    groups = parsed.get("candidates")
    if not isinstance(groups, list):
        raise ValueError("Candidate response must contain a candidates array")
    seen_groups: set[str] = set()
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("Each candidate group must be an object")
        slot_id = str(group.get("slot_id") or "")
        if slot_id not in allowed_slots:
            raise ValueError(f"Unknown slot ID: {slot_id}")
        if slot_id in seen_groups:
            raise ValueError(f"Duplicate candidate group for {slot_id}")
        seen_groups.add(slot_id)
        raw_items = group.get("items")
        if not isinstance(raw_items, list) or len(raw_items) != per_slot:
            raise ValueError(f"Expected exactly {per_slot} candidates for {slot_id}")
        accepted_ranges: list[tuple[float, float]] = []
        excluded_ranges = [
            parse_range(timestamp)
            for timestamp in excluded_ranges_by_slot.get(slot_id, [])
        ]
        segments = source_segments_by_slot[slot_id]
        allowed_ranges = _segment_ranges(segments)
        available_shots = [
            shot
            for segment in segments
            for shot in segment["shots"]
        ]
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise ValueError(f"Candidate for {slot_id} must be an object")
            start, end = parse_range(str(raw.get("timestamp") or ""))
            planned_duration = float(slots_by_id[slot_id]["planned_duration_sec"])
            if abs((end - start) - planned_duration) > _TIMESTAMP_TOLERANCE_SEC:
                raise ValueError(
                    f"Candidate for {slot_id} duration must equal "
                    f"planned_duration_sec={planned_duration:.6f}"
                )
            candidate_range = (start, end)
            if not any(
                start >= allowed_start - _TIMESTAMP_TOLERANCE_SEC
                and end <= allowed_end + _TIMESTAMP_TOLERANCE_SEC
                for allowed_start, allowed_end in allowed_ranges
            ):
                raise ValueError(
                    f"Candidate for {slot_id} is outside the supplied Segment timeline"
                )
            if any(
                _ranges_overlap(candidate_range, existing)
                for existing in [*accepted_ranges, *excluded_ranges]
            ):
                raise ValueError(
                    f"Candidate time ranges overlap for {slot_id}"
                )
            accepted_ranges.append(candidate_range)
            normalized_range = format_range(start, end)
            source_shot_ids = [
                str(shot["shot_id"])
                for shot in available_shots
                if _ranges_overlap(
                    candidate_range,
                    (
                        float(shot["time_range"]["start_sec"]),
                        float(shot["time_range"]["end_sec"]),
                    ),
                )
            ]
            if not source_shot_ids:
                raise ValueError(
                    f"Candidate for {slot_id} does not overlap a supplied Shot"
                )
            source_segment_ids = [
                str(segment["segment_id"])
                for segment in segments
                if _ranges_overlap(
                    candidate_range,
                    (
                        float(segment["time_range"]["start_sec"]),
                        float(segment["time_range"]["end_sec"]),
                    ),
                )
            ]
            description = str(raw.get("description") or "").strip()
            if not description:
                raise ValueError(f"Candidate for {slot_id} lacks structured visual context")
            result[slot_id].append(
                {
                    "candidate_id": f"{slot_id}_candidate_{len(result[slot_id]) + 1:02d}",
                    "slot_id": slot_id,
                    "timestamp": normalized_range,
                    "source_segment_ids": source_segment_ids,
                    "source_shot_ids": source_shot_ids,
                    "structured_context": description,
                    "description": description,
                    "semantic_relevance": max(
                        0.0, min(1.0, float(raw["semantic_relevance"]))
                    ),
                    "emotional_intensity": max(
                        0.0, min(1.0, float(raw["emotional_intensity"]))
                    ),
                    "salience": max(0.0, min(1.0, float(raw["salience"]))),
                }
            )
    missing_groups = allowed_slots - seen_groups
    if missing_groups:
        raise ValueError(f"Missing candidate groups: {sorted(missing_groups)}")
    return result

def _validate_visual_grounding(
    parsed: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    expected = {candidate["candidate_id"] for candidate in candidates}
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
        visibility_likert = float(raw["required_subject_visibility"])
        if (
            not visibility_likert.is_integer()
            or visibility_likert < 1
            or visibility_likert > 5
        ):
            raise ValueError(
                f"required_subject_visibility for {candidate_id} must be an integer from 1 to 5"
            )
        relevance_likert = float(raw["visual_slot_relevance"])
        if (
            not relevance_likert.is_integer()
            or relevance_likert < 1
            or relevance_likert > 5
        ):
            raise ValueError(
                f"visual_slot_relevance for {candidate_id} must be an integer from 1 to 5"
            )
        result[candidate_id] = {
            "description": description,
            "visible_subjects": [
                str(value).strip()
                for value in raw.get("visible_subjects") or []
                if str(value).strip()
            ],
            "protagonist_visibility_likert": int(visibility_likert),
            "visual_slot_relevance_likert": int(relevance_likert),
            "visual_evidence": str(raw["visual_evidence"]).strip(),
        }
    if set(result) != expected:
        raise ValueError(f"Visual validation omitted IDs: {sorted(expected - set(result))}")
    return result


def _candidate_segment_video_descriptions(
    candidate: dict[str, Any],
    video_description: dict[str, Any],
) -> list[dict[str, Any]]:
    candidate_range = parse_range(candidate["timestamp"])
    requested_segment_ids = {
        str(segment_id) for segment_id in candidate["source_segment_ids"]
    }
    result: list[dict[str, Any]] = []
    for segment in video_description["segments"]:
        if str(segment["segment_id"]) not in requested_segment_ids:
            continue
        segment_context = {
            key: segment[key]
            for key in (
                "segment_id",
                "time_range",
                "content_type",
                "timeline_role",
                "dialogue_context",
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
                )
                if key in shot
            }
            for shot in segment["shots"]
            if _ranges_overlap(
                candidate_range,
                (
                    float(shot["time_range"]["start_sec"]),
                    float(shot["time_range"]["end_sec"]),
                ),
            )
        ]
        candidate_dialogues: dict[str, dict[str, Any]] = {}
        for shot in segment["shots"]:
            for dialogue in shot.get("dialogue", []):
                dialogue_range = (
                    float(dialogue["time_range"]["start_sec"]),
                    float(dialogue["time_range"]["end_sec"]),
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
            key=lambda dialogue: float(dialogue["time_range"]["start_sec"]),
        )
        result.append(segment_context)
    return result


def add_visual_features(
    media: SegmentMediaReader,
    slots: list[dict[str, Any]],
    pool: dict[str, list[dict[str, Any]]],
    config: VLMConfig,
    context: WorkflowContext,
    *,
    sample_frames: int,
    operation: str,
    show_progress: bool = True,
) -> None:
    candidates = [candidate for slot in slots for candidate in pool[slot["slot_id"]]]
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(candidates)))) as executor:
        image_results = executor.map(
            lambda candidate: _contact_sheet_data_url(
                media, candidate, sample_frames
            ),
            candidates,
        )
        candidate_image_data_urls = (
            list(
                progress_iter(
                    image_results,
                    total=len(candidates),
                    description="Candidate contact sheets",
                    unit="candidate",
                )
            )
            if show_progress
            else list(image_results)
        )
    slots_by_id = {slot["slot_id"]: slot for slot in slots}
    video_description = context.get_artifact("video_description")
    if video_description is None:
        raise RuntimeError(
            "Video description must be available before candidate visual validation"
        )

    def candidate_spec(candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            "candidate_id": candidate["candidate_id"],
            "slot_id": candidate["slot_id"],
            "intended_visible_content": slots_by_id[candidate["slot_id"]][
                "content_description"
            ],
            "required_visible_subjects": slots_by_id[candidate["slot_id"]].get(
                "required_visible_subjects", []
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
        image_urls: list[str],
        suffix: str = "",
        *,
        resampled: bool = False,
    ) -> dict[str, dict[str, Any]]:
        subset_operation = operation + suffix
        package = prompt_registry.build(
            PromptStage.PLANNER,
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
                image_data_urls=image_urls,
                image_labels=[candidate["candidate_id"] for candidate in subset],
            )
        except Exception as exc:
            if "data_inspection_failed" not in str(exc).lower():
                raise
            if len(subset) > 1:
                midpoint = len(subset) // 2
                log_event(
                    "WARNING",
                    "planner.candidate",
                    "fallback.apply",
                    "Image-inspection batch was rejected; splitting candidates",
                    operation=subset_operation,
                    candidates=len(subset),
                    fallback="split_batch",
                    error_type=type(exc).__name__,
                    reason=error_summary(exc),
                )
                return {
                    **score_subset(
                        subset[:midpoint],
                        image_urls[:midpoint],
                        f"{suffix} split A",
                    ),
                    **score_subset(
                        subset[midpoint:],
                        image_urls[midpoint:],
                        f"{suffix} split B",
                    ),
                }
            if not resampled:
                candidate = subset[0]
                log_event(
                    "WARNING",
                    "planner.candidate",
                    "fallback.apply",
                    "Image-inspection candidate was rejected; resampling one frame",
                    operation=subset_operation,
                    candidate_id=candidate["candidate_id"],
                    fallback="resample_single_frame",
                    error_type=type(exc).__name__,
                    reason=error_summary(exc),
                )
                return score_subset(
                    subset,
                    [_contact_sheet_data_url(media, candidate, 1)],
                    f"{suffix} resampled",
                    resampled=True,
                )
            raise

    if show_progress:
        with progress_bar(
            total=len(candidates),
            description="Candidate VLM validation",
            unit="candidate",
        ) as progress:
            grounded = score_subset(candidates, candidate_image_data_urls)
            progress.update(len(candidates))
    else:
        grounded = score_subset(candidates, candidate_image_data_urls)
    for candidate in candidates:
        candidate.update(grounded[candidate["candidate_id"]])

def _retrieval_segment_context(
    video_description: dict[str, Any],
    slots: list[dict[str, Any]],
    *,
    include_adjacent: bool,
) -> dict[str, list[dict[str, Any]]]:
    segments = video_description["segments"]
    segment_positions = {
        str(segment["segment_id"]): index
        for index, segment in enumerate(segments)
    }
    radius = 1 if include_adjacent else 0
    result: dict[str, list[dict[str, Any]]] = {}
    for slot in slots:
        selected_positions: set[int] = set()
        for segment_id in slot["source_segment_ids"]:
            position = segment_positions[str(segment_id)]
            selected_positions.update(
                range(
                    max(0, position - radius),
                    min(len(segments), position + radius + 1),
                )
            )
        result[slot["slot_id"]] = [
            {
                **{
                    key: value
                    for key, value in segments[position].items()
                    if key not in {"clip_path", "dialogue_context"}
                },
                "shots": [
                    {
                        key: value
                        for key, value in shot.items()
                        if key not in {
                            "dialogue",
                            "sampled_frame_times_sec",
                        }
                    }
                    for shot in segments[position]["shots"]
                ],
            }
            for position in sorted(selected_positions)
        ]
    return result


def retrieve_candidates(
    slots: list[dict[str, Any]],
    media: SegmentMediaReader,
    config: LLMConfig,
    vlm_config: VLMConfig,
    retrieval_config: CandidateRetrievalConfig,
    context: WorkflowContext,
    *,
    replan_slots: Callable[
        [list[dict[str, Any]], list[dict[str, Any]]],
        tuple[list[dict[str, Any]], set[str]],
    ]
    | None = None,
) -> dict[str, list[dict[str, Any]]]:
    pool: dict[str, list[dict[str, Any]]] = {
        slot["slot_id"]: (
            [dict(slot["fixed_candidate"])]
            if slot.get("fixed_candidate") is not None
            else []
        )
        for slot in slots
    }
    fixed_slot_ids = {
        str(slot["slot_id"])
        for slot in slots
        if slot.get("fixed_candidate") is not None
    }
    video_description = context.get_artifact("video_description")
    if video_description is None:
        raise RuntimeError("Video description must be available before candidate retrieval")
    rejected: list[dict[str, Any]] = []
    primary_rounds = retrieval_config.retrieval_max_rounds + 1
    adjacent_rounds = retrieval_config.retrieval_max_rounds
    total_rounds = primary_rounds + adjacent_rounds
    primary_scope_exhausted: set[str] = set()
    adjacent_scope_exhausted: set[str] = set()
    freshly_replanned: set[str] = set()
    for round_index in range(1, total_rounds + 1):
        include_adjacent = round_index > primary_rounds
        scope = "adjacent_segments" if include_adjacent else "planned_segments"
        scope_round = (
            round_index - primary_rounds
            if include_adjacent
            else round_index
        )
        exhausted_slots = (
            adjacent_scope_exhausted
            if include_adjacent
            else primary_scope_exhausted
        )
        pending = [
            slot
            for slot in slots
            if slot["slot_id"] not in fixed_slot_ids
            if len(pool[slot["slot_id"]]) < retrieval_config.candidates_per_slot
            if slot["slot_id"] not in exhausted_slots
        ]
        if not pending:
            if all(
                slot["slot_id"] in fixed_slot_ids
                or len(pool[slot["slot_id"]])
                >= retrieval_config.candidates_per_slot
                for slot in slots
            ):
                break
            continue
        source_segments_by_slot: dict[str, list[dict[str, Any]]] = {}
        for slot in pending:
            slot_id = str(slot["slot_id"])
            source_segments_by_slot.update(
                _retrieval_segment_context(
                    video_description,
                    [slot],
                    include_adjacent=(
                        include_adjacent and slot_id not in freshly_replanned
                    ),
                )
            )
        freshly_replanned.difference_update(
            str(slot["slot_id"]) for slot in pending
        )
        excluded = {
            slot["slot_id"]: [
                item["timestamp"] for item in pool[slot["slot_id"]]
            ]
            + [
                item["timestamp"]
                for item in rejected
                if item["slot_id"] == slot["slot_id"]
            ]
            for slot in pending
        }

        def retrieve_slot(
            slot: dict[str, Any],
        ) -> tuple[
            str,
            dict[str, list[dict[str, Any]]] | None,
            dict[str, Any] | None,
        ]:
            slot_id = slot["slot_id"]
            slot_segments = {slot_id: source_segments_by_slot[slot_id]}
            candidates_needed = (
                retrieval_config.candidates_per_slot - len(pool[slot_id])
            )
            available_capacity = _window_capacity(
                slot_segments[slot_id],
                float(slot["planned_duration_sec"]),
                excluded[slot_id],
            )
            if available_capacity < candidates_needed:
                capacity_failure = {
                    "slot_id": slot_id,
                    "reason": "insufficient_non_overlapping_capacity",
                    "round": round_index,
                    "scope": scope,
                    "scope_round": scope_round,
                    "available_capacity": available_capacity,
                    "candidates_needed": candidates_needed,
                    "excluded_ranges": excluded[slot_id],
                    "source_segment_ids": list(slot["source_segment_ids"]),
                }
                log_event(
                    "WARNING",
                    "planner.candidate",
                    "fallback.apply",
                    (
                        "Candidate scope lacks enough non-overlapping fixed-duration "
                        "windows; queuing targeted Slot replanning"
                        if replan_slots is not None
                        else "Candidate scope lacks enough non-overlapping fixed-duration "
                        "windows; expanding in the next round"
                    ),
                    round=round_index,
                    scope=scope,
                    scope_round=scope_round,
                    slot_id=slot_id,
                    candidates_needed=candidates_needed,
                    available_capacity=available_capacity,
                    planned_duration_sec=float(slot["planned_duration_sec"]),
                )
                return slot_id, None, capacity_failure
            confirmed_candidates = {
                slot_id: [
                    {
                        "candidate_id": candidate["candidate_id"],
                        "timestamp": candidate["timestamp"],
                        "visible_description": candidate["description"],
                        "visual_evidence": candidate["visual_evidence"],
                    }
                    for candidate in pool[slot_id]
                ]
            }
            package = prompt_registry.build(
                PromptStage.PLANNER,
                PromptTask.CANDIDATE_RETRIEVAL,
                CandidateRetrievalDetails(
                    operation=f"Candidate retrieval round {round_index} slot {slot_id}",
                    candidates_per_slot=candidates_needed,
                    slots=[slot],
                    confirmed_candidates=confirmed_candidates,
                    excluded_ranges={slot_id: excluded[slot_id]},
                    source_segments_by_slot=slot_segments,
                ),
            )
            try:
                slot_pool = context.call_prompt(
                    package=package,
                    config=config,
                    validate_business=lambda parsed: _validate_candidates(
                        parsed,
                        [slot],
                        slot_segments,
                        candidates_needed,
                        {slot_id: excluded[slot_id]},
                    ),
                )
            except Exception as exc:
                log_event(
                    "WARNING",
                    "planner.candidate",
                    "validation.reject",
                    "Slot candidate retrieval failed; expanding in the next round",
                    round=round_index,
                    scope=scope,
                    scope_round=scope_round,
                    slot_id=slot_id,
                    error_type=type(exc).__name__,
                    reason=error_summary(exc),
                )
                return slot_id, None, None
            for item_index, candidate in enumerate(slot_pool[slot_id], 1):
                candidate["candidate_id"] = (
                    f"{slot_id}_round_{round_index:02d}_candidate_{item_index:02d}"
                )
            return slot_id, slot_pool, None

        llm_workers = max(1, min(config.max_concurrency, len(pending)))
        log_event(
            "INFO",
            "planner.candidate",
            "stage.progress",
            "Per-Slot candidate retrieval concurrency configured",
            round=round_index,
            scope=scope,
            scope_round=scope_round,
            slots=len(pending),
            workers=llm_workers,
        )
        with ThreadPoolExecutor(
            max_workers=llm_workers,
            thread_name_prefix="candidate-llm",
        ) as executor:
            slot_results = list(
                progress_iter(
                    executor.map(retrieve_slot, pending),
                    total=len(pending),
                    description=f"Candidate LLM retrieval round {round_index}",
                    unit="slot",
                )
            )

        exhausted_slots.update(
            slot_id
            for slot_id, _slot_pool, capacity_failure in slot_results
            if capacity_failure is not None
        )
        capacity_failures = [
            capacity_failure
            for _slot_id, _slot_pool, capacity_failure in slot_results
            if capacity_failure is not None
        ]
        round_pool = {
            slot_id: slot_pool[slot_id]
            for slot_id, slot_pool, _capacity_failure in slot_results
            if slot_pool is not None
        }
        successful_slots = [
            slot for slot in pending if slot["slot_id"] in round_pool
        ]
        if successful_slots:
            add_kinetic_features(
                media,
                round_pool,
                retrieval_config.motion_sample_fps,
                retrieval_config.motion_workers,
            )
            for slot in successful_slots:
                slot_id = str(slot["slot_id"])
                moving_candidates: list[dict[str, Any]] = []
                for candidate in round_pool[slot_id]:
                    kinetic_energy = float(candidate["kinetic_energy"])
                    if kinetic_energy > _VISUALLY_STATIC_MAX_KINETIC_ENERGY:
                        moving_candidates.append(candidate)
                        continue
                    log_event(
                        "WARNING",
                        "planner.candidate",
                        "validation.reject",
                        "Candidate rejected by local motion diagnostics",
                        round=round_index,
                        scope=scope,
                        scope_round=scope_round,
                        slot_id=slot_id,
                        candidate_id=candidate["candidate_id"],
                        timestamp=candidate["timestamp"],
                        reason="visually_static",
                        kinetic_energy=kinetic_energy,
                        static_threshold=_VISUALLY_STATIC_MAX_KINETIC_ENERGY,
                    )
                    rejected.append(
                        {
                            "slot_id": slot_id,
                            "timestamp": candidate["timestamp"],
                            "candidate_id": candidate["candidate_id"],
                            "reason": "visually_static",
                            "diagnostic_source": "local_motion",
                            "planned_content_description": slot[
                                "content_description"
                            ],
                            "kinetic_energy": kinetic_energy,
                            "static_threshold": (
                                _VISUALLY_STATIC_MAX_KINETIC_ENERGY
                            ),
                        }
                    )
                round_pool[slot_id] = moving_candidates

            visual_slots = [
                slot
                for slot in successful_slots
                if round_pool[str(slot["slot_id"])]
            ]

            def validate_slot_visuals(slot: dict[str, Any]) -> None:
                slot_id = slot["slot_id"]
                add_visual_features(
                    media,
                    [slot],
                    {slot_id: round_pool[slot_id]},
                    vlm_config,
                    context,
                    sample_frames=retrieval_config.visual_sample_frames,
                    operation=(
                        f"Visual candidate validation round {round_index} slot {slot_id}"
                    ),
                    show_progress=False,
                )

            vlm_workers = max(
                1,
                min(vlm_config.max_concurrency, len(visual_slots)),
            )
            if visual_slots:
                with ThreadPoolExecutor(
                    max_workers=vlm_workers,
                    thread_name_prefix="candidate-vlm",
                ) as executor:
                    list(
                        progress_iter(
                            executor.map(validate_slot_visuals, visual_slots),
                            total=len(visual_slots),
                            description=f"Candidate VLM validation round {round_index}",
                            unit="slot",
                        )
                    )

            for slot in visual_slots:
                slot_id = slot["slot_id"]
                requires_subject = bool(slot.get("required_visible_subjects"))
                for candidate in round_pool[slot_id]:
                    visibility = _normalize_likert_score(
                        candidate["protagonist_visibility_likert"]
                    )
                    overlap = any(
                        _ranges_overlap(
                            parse_range(candidate["timestamp"]),
                            parse_range(existing["timestamp"]),
                        )
                        for existing in pool[slot_id]
                    )
                    visibility_ok = (
                        not requires_subject
                        or visibility
                        >= retrieval_config.protagonist_visibility_threshold
                    )
                    if overlap or not visibility_ok:
                        rejection_reason = (
                            "overlapping_range"
                            if overlap
                            else "required_subject_not_visually_confirmed"
                        )
                        log_event(
                            "WARNING",
                            "planner.candidate",
                            "validation.reject",
                            "Candidate rejected after visual diagnostics",
                            round=round_index,
                            scope=scope,
                            scope_round=scope_round,
                            slot_id=slot_id,
                            candidate_id=candidate["candidate_id"],
                            timestamp=candidate["timestamp"],
                            reason=rejection_reason,
                            required_visible_subjects=list(
                                slot.get("required_visible_subjects") or []
                            ),
                            protagonist_visibility_likert=candidate[
                                "protagonist_visibility_likert"
                            ],
                            protagonist_visibility=visibility,
                            visibility_threshold=(
                                retrieval_config.protagonist_visibility_threshold
                            ),
                            kinetic_energy=candidate["kinetic_energy"],
                            visual_evidence=candidate["visual_evidence"],
                        )
                        rejected.append(
                            {
                                "slot_id": slot_id,
                                "timestamp": candidate["timestamp"],
                                "candidate_id": candidate["candidate_id"],
                                "reason": rejection_reason,
                                "planned_content_description": slot[
                                    "content_description"
                                ],
                                "required_visible_subjects": list(
                                    slot.get("required_visible_subjects") or []
                                ),
                                "visible_description": candidate["description"],
                                "visible_subjects": list(
                                    candidate.get("visible_subjects") or []
                                ),
                                "visual_slot_relevance_likert": candidate.get(
                                    "visual_slot_relevance_likert"
                                ),
                                "protagonist_visibility_likert": candidate[
                                    "protagonist_visibility_likert"
                                ],
                                "protagonist_visibility": visibility,
                                "kinetic_energy": candidate["kinetic_energy"],
                                "visual_evidence": candidate["visual_evidence"],
                            }
                        )
                        continue
                    pool[slot_id].append(candidate)

        targeted_failures = list(capacity_failures)
        for slot in successful_slots:
            slot_id = str(slot["slot_id"])
            if pool[slot_id]:
                continue
            slot_rejections = [
                item
                for item in rejected
                if item["slot_id"] == slot_id
            ]
            targeted_failures.append(
                {
                    "slot_id": slot_id,
                    "reason": "no_candidate_passed_visual_diagnostics",
                    "round": round_index,
                    "scope": scope,
                    "scope_round": scope_round,
                    "source_segment_ids": list(slot["source_segment_ids"]),
                    "planned_content_description": slot["content_description"],
                    "required_visible_subjects": list(
                        slot.get("required_visible_subjects") or []
                    ),
                    "candidate_rejections": slot_rejections,
                }
            )

        if replan_slots is not None and targeted_failures:
            target_slot_ids = {
                str(failure["slot_id"]) for failure in targeted_failures
            }
            log_event(
                "WARNING",
                "planner.slot",
                "fallback.apply",
                "Redesigning failed Slots in one targeted planning call",
                round=round_index,
                slot_ids=sorted(target_slot_ids),
                reasons={
                    failure["slot_id"]: failure["reason"]
                    for failure in targeted_failures
                },
            )
            redesigned_slots, replanned_slot_ids = replan_slots(
                slots,
                targeted_failures,
            )
            slots[:] = redesigned_slots
            for slot_id in replanned_slot_ids:
                pool[slot_id] = []
                primary_scope_exhausted.discard(slot_id)
                adjacent_scope_exhausted.discard(slot_id)
            rejected = [
                item
                for item in rejected
                if item["slot_id"] not in replanned_slot_ids
            ]
            freshly_replanned.update(replanned_slot_ids)
            log_event(
                "INFO",
                "planner.slot",
                "stage.complete",
                "Targeted Slot replanning completed; retrying replanned Slots",
                round=round_index,
                failed_slot_ids=sorted(target_slot_ids),
                slot_ids=sorted(replanned_slot_ids),
            )
            continue
    shortages = {
        slot_id: retrieval_config.candidates_per_slot - len(candidates)
        for slot_id, candidates in pool.items()
        if slot_id not in fixed_slot_ids
        if len(candidates) < retrieval_config.candidates_per_slot
    }
    context.set_artifact("candidate_rejections", rejected)
    if shortages:
        failure = {
            "reason": "insufficient_visually_grounded_candidates",
            "shortages": shortages,
            "planned_segment_rounds": primary_rounds,
            "adjacent_expansion_rounds": adjacent_rounds,
        }
        context.set_artifact("retrieval_failure", failure)
        empty_slots = [
            slot_id
            for slot_id in shortages
            if not pool[slot_id]
        ]
        if empty_slots:
            raise ValueError(
                "No usable candidate remains after visual diagnostics for Slots: "
                + json.dumps(empty_slots, ensure_ascii=False)
            )
        log_event(
            "ERROR",
            "planner.candidate",
            "validation.reject",
            "Candidate retrieval exhausted; continuing with smaller candidate pools",
            shortages=shortages,
            planned_segment_rounds=primary_rounds,
            adjacent_expansion_rounds=adjacent_rounds,
        )
    for slot_id, candidates in pool.items():
        candidates.sort(key=lambda item: parse_range(item["timestamp"])[0])
        for index, candidate in enumerate(candidates, 1):
            if slot_id not in fixed_slot_ids:
                candidate["candidate_id"] = f"{slot_id}_candidate_{index:02d}"
    add_kinetic_features(
        media,
        {
            slot_id: pool[slot_id]
            for slot_id in fixed_slot_ids
        },
        retrieval_config.motion_sample_fps,
        retrieval_config.motion_workers,
    )
    context.set_artifact("candidate_pool", pool)
    return pool


def _candidate_motion(
    media: SegmentMediaReader,
    start: float,
    end: float,
    fps: float,
) -> float:
    values: list[float] = []
    sample_step = 1.0 / max(fps, 0.1)
    sample_times: list[float] = []
    next_sample = start
    while next_sample < end:
        sample_times.append(next_sample)
        next_sample += sample_step
    previous = None
    for frame in media.sample_frames(sample_times):
        gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
        if previous is not None:
            values.append(float(cv2.absdiff(gray, previous).mean()) / 255.0)
        previous = gray
    if not values:
        return 0.0
    raw = sum(values) / len(values)
    return max(0.0, min(1.0, raw / 0.18))


def add_kinetic_features(
    media: SegmentMediaReader,
    pool: dict[str, list[dict[str, Any]]],
    sample_fps: float,
    max_workers: int = 4,
) -> None:
    candidates = [candidate for values in pool.values() for candidate in values]
    if not candidates:
        return
    worker_count = max(1, min(max_workers, len(candidates)))
    progress = progress_bar(
        total=len(candidates),
        description="Candidate motion analysis",
        unit="candidate",
    )

    def process(candidate: dict[str, Any]) -> None:
        start, end = parse_range(candidate["timestamp"])
        candidate["kinetic_energy"] = round(
            _candidate_motion(media, start, end, sample_fps), 4
        )
        progress.update()

    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            list(executor.map(process, candidates))
    finally:
        progress.close()
