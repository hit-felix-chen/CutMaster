from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
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
from cutmaster.infrastructure.observability.progress import (
    progress_bar,
    progress_iter,
)
from cutmaster.workflow.planners.edit_composer import score_unary_candidate
from cutmaster.workflow.planners.tools.errors import (
    TargetedRepairUnrepairableError,
)
from cutmaster.workflow.planners.tools.segment_media import SegmentMediaReader
from cutmaster.workflow.planners.tools.visual_scoring import _contact_sheet_data_url
from cutmaster.workflow.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.workflow.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)
from cutmaster.workflow.prompting.planners import (
    CandidateRetrievalDetails,
    CandidateVisualScoringDetails,
)
from cutmaster.workflow.shared.execution_context import WorkflowContext
from cutmaster.workflow.shared.timecode import format_range, parse_range


_TIMESTAMP_TOLERANCE_SEC = 0.0011
_ADJACENT_CANDIDATE_MULTIPLIER = 3


_SLOT_RETRIEVAL_SIGNATURE_KEYS = (
    "narrative_role",
    "content_description",
    "target_emotion",
    "target_emotional_intensity",
    "target_kinetic_energy",
    "desired_duration_sec",
    "planned_duration_sec",
    "continuity_from_previous",
    "source_segment_ids",
    "required_visible_subjects",
    "fixed_candidate",
)


def _slot_retrieval_signature(slot: dict[str, Any]) -> str:
    """Return the part of a Slot that can change retrieval or validation."""

    return json.dumps(
        {
            key: slot.get(key)
            for key in _SLOT_RETRIEVAL_SIGNATURE_KEYS
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _ranges_overlap(
    first: tuple[float, float],
    second: tuple[float, float],
) -> bool:
    return (
        first[0] < second[1] - _TIMESTAMP_TOLERANCE_SEC
        and second[0] < first[1] - _TIMESTAMP_TOLERANCE_SEC
    )


def _segment_ranges(segments: list[dict[str, Any]]) -> list[tuple[float, float]]:
    return [
        (
            float(segment["time_range"]["start_sec"]),
            float(segment["time_range"]["end_sec"]),
        )
        for segment in segments
    ]


def _usable_segment_count(
    segments: list[dict[str, Any]],
    duration_sec: float,
) -> int:
    return sum(
        float(segment["time_range"]["end_sec"])
        - float(segment["time_range"]["start_sec"])
        > duration_sec + _TIMESTAMP_TOLERANCE_SEC
        for segment in segments
    )


def _same_range(
    first: tuple[float, float],
    second: tuple[float, float],
) -> bool:
    return (
        abs(first[0] - second[0]) <= _TIMESTAMP_TOLERANCE_SEC
        and abs(first[1] - second[1]) <= _TIMESTAMP_TOLERANCE_SEC
    )


def _candidate_overlap_shot_ids(candidate: dict[str, Any]) -> set[str]:
    raw_shot_ids = candidate.get("overlap_shot_ids")
    if raw_shot_ids is None:
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
    if candidate_shot_ids and existing_shot_ids:
        if not candidate_shot_ids.intersection(existing_shot_ids):
            return False
    return _ranges_overlap(
        parse_range(candidate["timestamp"]),
        parse_range(existing["timestamp"]),
    )


def _all_static_batch_source_segment_id(
    candidates: list[dict[str, Any]],
    static_candidates: list[dict[str, Any]],
) -> str | None:
    """Return the one Segment proven unusable by an all-static candidate batch."""

    if not candidates or len(static_candidates) != len(candidates):
        return None
    assignments = {
        tuple(str(value) for value in candidate.get("source_segment_ids") or [])
        for candidate in candidates
    }
    if len(assignments) != 1:
        return None
    assignment = next(iter(assignments))
    if len(assignment) != 1:
        return None
    return assignment[0]


def _candidate_uses_any_source_segment(
    candidate: dict[str, Any],
    source_segment_ids: set[str],
) -> bool:
    candidate_segment_ids = {
        str(value)
        for value in candidate.get("source_segment_ids") or []
        if str(value)
    }
    return bool(candidate_segment_ids.intersection(source_segment_ids))


def _repair_blockers_for_exhausted_domain(
    slots: list[dict[str, Any]],
    failed_slot_ids: set[str],
    current_repair_slot_ids: set[str],
) -> tuple[dict[str, set[str]], set[str]]:
    """Return one deterministic adjacent expansion layer for a failed domain.

    The mapping is an explicit chain so Arrangement can verify that every
    absorbed blocker is immediately adjacent.  Non-dialogue fixed candidates
    are hard barriers.  A failed Slot with no movable boundary is returned in
    the second tuple and is backend-unrepairable locally.
    """

    slot_ids = [str(slot["slot_id"]) for slot in slots]
    slot_by_id = {str(slot["slot_id"]): slot for slot in slots}
    position = {slot_id: index for index, slot_id in enumerate(slot_ids)}
    blockers: dict[str, set[str]] = {}
    blocked_failures: set[str] = set()

    for failed_slot_id in sorted(failed_slot_ids, key=position.__getitem__):
        found_expansion = False
        for direction in (-1, 1):
            current_slot_id = failed_slot_id
            current_index = position[current_slot_id]
            while True:
                next_index = current_index + direction
                if next_index < 0 or next_index >= len(slots):
                    break
                next_slot_id = slot_ids[next_index]
                next_slot = slot_by_id[next_slot_id]
                if (
                    next_slot.get("fixed_candidate") is not None
                    and next_slot.get("dialogue_anchor") is None
                ):
                    break
                blockers.setdefault(current_slot_id, set()).add(next_slot_id)
                if next_slot_id not in current_repair_slot_ids:
                    found_expansion = True
                    break
                current_slot_id = next_slot_id
                current_index = next_index
        if not found_expansion:
            blocked_failures.add(failed_slot_id)

    return blockers, blocked_failures


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
                _same_range(candidate_range, existing)
                for existing in [*accepted_ranges, *excluded_ranges]
            ):
                raise ValueError(
                    f"Candidate time range is duplicated for {slot_id}"
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
        slot = slots_by_id[candidate["slot_id"]]
        return {
            "candidate_id": candidate["candidate_id"],
            "slot_id": candidate["slot_id"],
            "intended_visible_content": slot["content_description"],
            "required_visible_subjects": slot.get("required_visible_subjects", []),
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
                image_data_urls=image_urls,
                image_labels=[candidate["candidate_id"] for candidate in subset],
            )
        except Exception as exc:
            if "data_inspection_failed" not in str(exc).lower():
                raise
            inspection_failure = build_prompt_failure(
                PromptFailureCode.PROVIDER_IMAGE_INSPECTION_FAILED,
                operation=subset_operation,
                error_message=error_summary(exc),
            )
            if len(subset) > 1:
                midpoint = len(subset) // 2
                log_event(
                    "WARNING",
                    "aster.timeline",
                    "fallback.apply",
                    "Image-inspection batch was rejected; splitting candidates",
                    candidates=len(subset),
                    fallback="split_batch",
                    error_type=type(exc).__name__,
                    **inspection_failure,
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
                    "aster.timeline",
                    "fallback.apply",
                    "Image-inspection candidate was rejected; resampling one frame",
                    candidate_id=candidate["candidate_id"],
                    fallback="resample_single_frame",
                    error_type=type(exc).__name__,
                    **inspection_failure,
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
                **segments[position],
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
    planners_feedback = context.get_artifact("planners_feedback") or {}
    unavailable_source_segment_ids = {
        str(value)
        for value in [
            *(planners_feedback.get("unavailable_source_segment_ids") or []),
            *(context.get_artifact("unavailable_source_segment_ids") or []),
        ]
        if str(value)
    }
    context.set_artifact(
        "unavailable_source_segment_ids",
        sorted(unavailable_source_segment_ids),
    )
    fixed_unavailable_slot_ids = sorted(
        str(slot["slot_id"])
        for slot in slots
        if slot.get("fixed_candidate") is not None
        and _candidate_uses_any_source_segment(
            slot["fixed_candidate"],
            unavailable_source_segment_ids,
        )
    )
    if fixed_unavailable_slot_ids:
        failure = {
            "reason_code": "fixed_candidate_uses_unavailable_source_segment",
            "failed_slot_ids": fixed_unavailable_slot_ids,
            "unavailable_source_segment_ids": sorted(
                unavailable_source_segment_ids
            ),
        }
        context.set_artifact("retrieval_failure", failure)
        raise ValueError(
            "Fixed candidates use unavailable source Segments for Slots: "
            + json.dumps(fixed_unavailable_slot_ids, ensure_ascii=False)
        )
    rejection_history: list[dict[str, Any]] = []
    active_rejections: list[dict[str, Any]] = []
    failed_slot_diagnostics_by_assignment: dict[
        tuple[str, tuple[str, ...]], dict[str, Any]
    ] = {}
    seen_rejection_identities_by_assignment: dict[
        tuple[str, tuple[str, ...]], set[str]
    ] = {}

    def record_failed_slot_diagnostic(
        slot: dict[str, Any],
        failure: dict[str, Any],
        *,
        missing_candidates: int,
    ) -> None:
        slot_id = str(slot["slot_id"])
        segment_ids = tuple(
            str(value) for value in slot.get("source_segment_ids") or []
        )
        key = (slot_id, segment_ids)
        previous = failed_slot_diagnostics_by_assignment.get(key, {})
        seen_rejections = seen_rejection_identities_by_assignment.setdefault(
            key,
            set(),
        )
        new_rejections: list[dict[str, Any]] = []
        reason_counts = Counter(previous.get("reason_counts") or {})
        for rejection in failure.get("candidate_rejections") or []:
            identity = json.dumps(
                rejection,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            if identity in seen_rejections:
                continue
            seen_rejections.add(identity)
            new_rejections.append(rejection)
            reason_counts[str(rejection.get("reason_code") or "unknown")] += 1
        if not reason_counts:
            reason_counts[str(failure.get("reason_code") or "unknown")] += 1
        merged_rejections = [
            *(previous.get("candidate_rejections") or []),
            *new_rejections,
        ]
        first_by_reason: dict[str, dict[str, Any]] = {}
        last_by_reason: dict[str, dict[str, Any]] = {}
        for rejection in merged_rejections:
            reason_code = str(rejection.get("reason_code") or "unknown")
            first_by_reason.setdefault(reason_code, rejection)
            last_by_reason[reason_code] = rejection
        representative_rejections: list[dict[str, Any]] = []
        for reason_code in sorted(first_by_reason):
            representative_rejections.append(first_by_reason[reason_code])
            if last_by_reason[reason_code] is not first_by_reason[reason_code]:
                representative_rejections.append(last_by_reason[reason_code])
        representative_ids = {id(item) for item in representative_rejections}
        for rejection in reversed(merged_rejections):
            if len(representative_rejections) >= 12:
                break
            if id(rejection) in representative_ids:
                continue
            representative_rejections.append(rejection)
            representative_ids.add(id(rejection))
        representative_rejections = representative_rejections[:12]
        failed_slot_diagnostics_by_assignment[key] = {
            "slot_id": slot_id,
            "content_description": str(slot["content_description"]),
            "required_visible_subjects": list(
                slot.get("required_visible_subjects") or []
            ),
            "source_segment_ids": list(segment_ids),
            "missing_candidates": missing_candidates,
            "reason_code": str(failure.get("reason_code") or ""),
            "reason_counts": dict(sorted(reason_counts.items())),
            "candidate_rejections": representative_rejections,
        }

    def failed_slot_diagnostics() -> list[dict[str, Any]]:
        return list(failed_slot_diagnostics_by_assignment.values())

    primary_rounds = retrieval_config.retrieval_max_rounds + 1
    adjacent_rounds = 1
    total_rounds = primary_rounds + adjacent_rounds
    primary_scope_exhausted: set[str] = set()
    adjacent_scope_exhausted: set[str] = set()
    targeted_replan_used: set[str] = set()
    expanded_repair_domains: set[frozenset[str]] = set()
    terminally_exhausted: set[str] = set()
    repair_validation_pending: set[str] = set()
    unrepairable_diagnostics: dict[str, Any] | None = None
    base_round_index = 1
    round_index = 0
    while repair_validation_pending or base_round_index <= total_rounds:
        round_index += 1
        slot_by_id = {str(slot["slot_id"]): slot for slot in slots}
        repair_validation_slot_ids = set(repair_validation_pending)
        repair_validation_pending.clear()
        is_repair_validation = bool(repair_validation_slot_ids)
        configured_round_index: int | None = None
        if is_repair_validation:
            include_adjacent = False
            scope = "targeted_repair_validation"
            scope_round = 1
            pending = [
                slot
                for slot in slots
                if slot["slot_id"] in repair_validation_slot_ids
                if slot["slot_id"] not in fixed_slot_ids
                if len(pool[slot["slot_id"]])
                < retrieval_config.candidates_per_slot
            ]
        else:
            configured_round_index = base_round_index
            base_round_index += 1
            include_adjacent = configured_round_index > primary_rounds
            scope = (
                "adjacent_segments" if include_adjacent else "planned_segments"
            )
            scope_round = (
                configured_round_index - primary_rounds
                if include_adjacent
                else configured_round_index
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
                if len(pool[slot["slot_id"]])
                < retrieval_config.candidates_per_slot
                if slot["slot_id"] not in exhausted_slots
                if slot["slot_id"] not in terminally_exhausted
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
            source_context = _retrieval_segment_context(
                video_description,
                [slot],
                include_adjacent=include_adjacent,
            )
            source_segments_by_slot[slot_id] = [
                segment
                for segment in source_context[slot_id]
                if str(segment["segment_id"])
                not in unavailable_source_segment_ids
            ]
        excluded = {
            slot["slot_id"]: [
                item["timestamp"] for item in pool[slot["slot_id"]]
            ]
            + [
                item["timestamp"]
                for item in rejection_history
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
            candidates_requested = (
                candidates_needed * _ADJACENT_CANDIDATE_MULTIPLIER
                if include_adjacent
                else candidates_needed
            )
            usable_segment_count = _usable_segment_count(
                slot_segments[slot_id],
                float(slot["planned_duration_sec"]),
            )
            if usable_segment_count == 0:
                longest_segment_duration_sec = max(
                    (
                        float(segment["time_range"]["end_sec"])
                        - float(segment["time_range"]["start_sec"])
                        for segment in slot_segments[slot_id]
                    ),
                    default=0.0,
                )
                duration_failure = build_prompt_failure(
                    PromptFailureCode.SOURCE_SEGMENTS_TOO_SHORT,
                    slot_id=slot_id,
                    round=round_index,
                    scope=scope,
                    scope_round=scope_round,
                    planned_duration_sec=float(slot["planned_duration_sec"]),
                    longest_segment_duration_sec=longest_segment_duration_sec,
                    source_segment_ids=list(slot["source_segment_ids"]),
                )
                log_event(
                    "WARNING",
                    "aster.timeline",
                    "fallback.apply",
                    (
                        "Candidate scope has no Segment longer than the planned clip; "
                        "queuing targeted Slot redesign"
                        if replan_slots is not None
                        else "Candidate scope has no Segment longer than the planned "
                        "clip; expanding in the next round"
                    ),
                    **duration_failure,
                )
                return slot_id, None, duration_failure
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
                PromptStage.PLANNERS,
                PromptTask.CANDIDATE_RETRIEVAL,
                CandidateRetrievalDetails(
                    operation=f"Candidate retrieval round {round_index} slot {slot_id}",
                    candidates_per_slot=candidates_requested,
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
                        candidates_requested,
                        {slot_id: excluded[slot_id]},
                    ),
                )
            except Exception as exc:
                retrieval_failure = build_prompt_failure(
                    PromptFailureCode.CANDIDATE_RETRIEVAL_FAILED,
                    slot_id=slot_id,
                    round=round_index,
                    scope=scope,
                    scope_round=scope_round,
                    error_type=type(exc).__name__,
                    error_message=error_summary(exc),
                )
                log_event(
                    "WARNING",
                    "aster.timeline",
                    "validation.reject",
                    (
                        "Targeted repair validation retrieval failed; handing the "
                        "transaction error to the outer ASTER attempt"
                        if is_repair_validation
                        else "Slot candidate retrieval failed; expanding in the next round"
                    ),
                    **retrieval_failure,
                )
                if is_repair_validation:
                    retrieval_failure.update(
                        {
                            "failed_slot_ids": [slot_id],
                            "failed_slot_diagnostics": failed_slot_diagnostics(),
                            "unavailable_source_segment_ids": sorted(
                                unavailable_source_segment_ids
                            ),
                        }
                    )
                    context.set_artifact("retrieval_failure", retrieval_failure)
                    raise ValueError(
                        "Candidate retrieval failed while validating a targeted "
                        f"repair for {slot_id}: {error_summary(exc)}"
                    ) from exc
                return slot_id, None, None
            for item_index, candidate in enumerate(slot_pool[slot_id], 1):
                candidate["candidate_id"] = (
                    f"{slot_id}_round_{round_index:02d}_candidate_{item_index:02d}"
                )
            return slot_id, slot_pool, None

        llm_workers = max(1, min(config.max_concurrency, len(pending)))
        log_event(
            "INFO",
            "aster.timeline",
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

        if not is_repair_validation:
            exhausted_slots.update(
                slot_id
                for slot_id, _slot_pool, duration_failure in slot_results
                if duration_failure is not None
            )
        duration_failures = [
            duration_failure
            for _slot_id, _slot_pool, duration_failure in slot_results
            if duration_failure is not None
        ]
        round_pool = {
            slot_id: slot_pool[slot_id]
            for slot_id, slot_pool, _duration_failure in slot_results
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
            newly_unavailable_segment_ids: set[str] = set()
            for slot in successful_slots:
                slot_id = str(slot["slot_id"])
                retrieved_candidates = list(round_pool[slot_id])
                moving_candidates: list[dict[str, Any]] = []
                static_candidates: list[dict[str, Any]] = []
                for candidate in round_pool[slot_id]:
                    kinetic_energy = float(candidate["kinetic_energy"])
                    static_threshold = (
                        retrieval_config.static_kinetic_energy_threshold
                    )
                    if kinetic_energy > static_threshold:
                        moving_candidates.append(candidate)
                        continue
                    static_failure = build_prompt_failure(
                        PromptFailureCode.VISUALLY_STATIC,
                        slot_id=slot_id,
                        candidate_id=candidate["candidate_id"],
                        timestamp=candidate["timestamp"],
                        kinetic_energy=kinetic_energy,
                        static_threshold=static_threshold,
                        round=round_index,
                        scope=scope,
                        scope_round=scope_round,
                        source_segment_ids=list(
                            candidate.get("source_segment_ids") or []
                        ),
                        source_shot_ids=sorted(
                            _candidate_overlap_shot_ids(candidate)
                        ),
                    )
                    log_event(
                        "WARNING",
                        "aster.timeline",
                        "validation.reject",
                        "Candidate rejected by local motion diagnostics",
                        **static_failure,
                    )
                    rejection = {
                        **static_failure,
                        "diagnostic_source": "local_motion",
                        "planned_content_description": slot[
                            "content_description"
                        ],
                    }
                    rejection_history.append(rejection)
                    active_rejections.append(rejection)
                    static_candidates.append(candidate)
                round_pool[slot_id] = moving_candidates

                static_segment_id = _all_static_batch_source_segment_id(
                    retrieved_candidates,
                    static_candidates,
                )
                if (
                    static_segment_id is not None
                    and static_segment_id not in unavailable_source_segment_ids
                ):
                    newly_unavailable_segment_ids.add(static_segment_id)

            if newly_unavailable_segment_ids:
                unavailable_source_segment_ids.update(
                    newly_unavailable_segment_ids
                )
                context.set_artifact(
                    "unavailable_source_segment_ids",
                    sorted(unavailable_source_segment_ids),
                )
                for segment_id in sorted(newly_unavailable_segment_ids):
                    log_event(
                        "WARNING",
                        "aster.timeline",
                        "validation.reject",
                        "Source Segment permanently excluded because every "
                        "candidate in one retrieval batch was static",
                        source_segment_id=segment_id,
                        confirmation_basis="all_static_candidate_batch",
                    )

                fixed_unavailable_slot_ids = sorted(
                    slot_id
                    for slot_id in fixed_slot_ids
                    if any(
                        _candidate_uses_any_source_segment(
                            candidate,
                            newly_unavailable_segment_ids,
                        )
                        for candidate in pool[slot_id]
                    )
                )
                if fixed_unavailable_slot_ids:
                    failure = {
                        "reason_code": (
                            "fixed_candidate_uses_unavailable_source_segment"
                        ),
                        "failed_slot_ids": fixed_unavailable_slot_ids,
                        "unavailable_source_segment_ids": sorted(
                            unavailable_source_segment_ids
                        ),
                    }
                    context.set_artifact("retrieval_failure", failure)
                    raise ValueError(
                        "Fixed candidates use unavailable source Segments for "
                        "Slots: "
                        + json.dumps(
                            fixed_unavailable_slot_ids,
                            ensure_ascii=False,
                        )
                    )

                def reject_invalidated_candidate(
                    invalidated_slot_id: str,
                    candidate: dict[str, Any],
                ) -> None:
                    matched_segment_ids = sorted(
                        {
                            str(segment_id)
                            for segment_id in candidate.get(
                                "source_segment_ids"
                            )
                            or []
                            if str(segment_id)
                            in newly_unavailable_segment_ids
                        }
                    )
                    rejection = {
                        "reason_code": PromptFailureCode.VISUALLY_STATIC.value,
                        "diagnosis": (
                            "Candidate invalidated because its Source Segment "
                            "was excluded by an all-static retrieval batch."
                        ),
                        "repair_requirement": (
                            "Retrieve the Slot from a different Source Segment."
                        ),
                        "slot_id": invalidated_slot_id,
                        "candidate_id": str(candidate.get("candidate_id") or ""),
                        "timestamp": str(candidate.get("timestamp") or ""),
                        "source_segment_ids": list(
                            candidate.get("source_segment_ids") or []
                        ),
                        "unavailable_source_segment_ids": matched_segment_ids,
                        "diagnostic_source": "source_segment_static_batch",
                        "planned_content_description": slot_by_id[
                            invalidated_slot_id
                        ]["content_description"],
                    }
                    rejection_history.append(rejection)
                    active_rejections.append(rejection)
                    log_event(
                        "WARNING",
                        "aster.timeline",
                        "validation.reject",
                        "Previously accepted candidate invalidated after its "
                        "Source Segment was excluded",
                        **rejection,
                    )

                for invalidated_slot_id, candidates in pool.items():
                    retained_candidates: list[dict[str, Any]] = []
                    for candidate in candidates:
                        if _candidate_uses_any_source_segment(
                            candidate,
                            newly_unavailable_segment_ids,
                        ):
                            reject_invalidated_candidate(
                                invalidated_slot_id,
                                candidate,
                            )
                            continue
                        retained_candidates.append(candidate)
                    pool[invalidated_slot_id] = retained_candidates

                for invalidated_slot_id, candidates in round_pool.items():
                    retained_candidates = []
                    for candidate in candidates:
                        if _candidate_uses_any_source_segment(
                            candidate,
                            newly_unavailable_segment_ids,
                        ):
                            reject_invalidated_candidate(
                                invalidated_slot_id,
                                candidate,
                            )
                            continue
                        retained_candidates.append(candidate)
                    round_pool[invalidated_slot_id] = retained_candidates

            for slot in successful_slots:
                slot_id = str(slot["slot_id"])
                moving_candidates = list(round_pool[slot_id])
                ranked_moving_candidates = sorted(
                    moving_candidates,
                    key=lambda candidate: (
                        -float(candidate["semantic_relevance"]),
                        -float(candidate["salience"]),
                        parse_range(candidate["timestamp"])[0],
                    ),
                )
                diverse_moving_candidates: list[dict[str, Any]] = []
                for candidate in ranked_moving_candidates:
                    duplicate_of = next(
                        (
                            existing
                            for existing in [
                                *pool[slot_id],
                                *diverse_moving_candidates,
                            ]
                            if _candidate_duplicates_source_evidence(
                                candidate,
                                existing,
                            )
                        ),
                        None,
                    )
                    if duplicate_of is None:
                        diverse_moving_candidates.append(candidate)
                        continue
                    duplicate_failure = build_prompt_failure(
                        PromptFailureCode.DUPLICATE_CANDIDATE_RANGE,
                        round=round_index,
                        scope=scope,
                        scope_round=scope_round,
                        slot_id=slot_id,
                        candidate_id=candidate["candidate_id"],
                        timestamp=candidate["timestamp"],
                        source_segment_ids=list(
                            candidate.get("source_segment_ids") or []
                        ),
                        source_shot_ids=sorted(
                            _candidate_overlap_shot_ids(candidate)
                        ),
                        conflicting_candidate_id=duplicate_of.get("candidate_id"),
                        conflicting_timestamp=duplicate_of.get("timestamp"),
                        conflicting_source_shot_ids=sorted(
                            _candidate_overlap_shot_ids(duplicate_of)
                        ),
                    )
                    rejection = {
                        **duplicate_failure,
                        "planned_content_description": slot["content_description"],
                        "diagnostic_source": "source_evidence_diversity",
                    }
                    rejection_history.append(rejection)
                    active_rejections.append(rejection)
                    log_event(
                        "WARNING",
                        "aster.timeline",
                        "validation.reject",
                        "Candidate rejected before VLM because its source evidence "
                        "duplicates another candidate",
                        **duplicate_failure,
                    )
                round_pool[slot_id] = diverse_moving_candidates

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
                grounded_candidates: list[dict[str, Any]] = []
                relevance_threshold = 3

                def reject_candidate(
                    candidate: dict[str, Any],
                    rejection_code: PromptFailureCode,
                ) -> None:
                    visual_failure = build_prompt_failure(
                        rejection_code,
                        round=round_index,
                        scope=scope,
                        scope_round=scope_round,
                        slot_id=slot_id,
                        candidate_id=candidate["candidate_id"],
                        timestamp=candidate["timestamp"],
                        required_visible_subjects=list(
                            slot.get("required_visible_subjects") or []
                        ),
                        visible_subjects=list(
                            candidate.get("visible_subjects") or []
                        ),
                        protagonist_visibility_likert=candidate[
                            "protagonist_visibility_likert"
                        ],
                        protagonist_visibility_likert_threshold=(
                            retrieval_config.protagonist_visibility_likert_threshold
                        ),
                        visual_slot_relevance_likert=candidate[
                            "visual_slot_relevance_likert"
                        ],
                        visual_slot_relevance_likert_threshold=(
                            relevance_threshold
                        ),
                        kinetic_energy=candidate["kinetic_energy"],
                        visual_evidence=candidate["visual_evidence"],
                        source_segment_ids=list(
                            candidate.get("source_segment_ids") or []
                        ),
                        source_shot_ids=sorted(
                            _candidate_overlap_shot_ids(candidate)
                        ),
                    )
                    log_event(
                        "WARNING",
                        "aster.timeline",
                        "validation.reject",
                        "Candidate rejected after visual diagnostics",
                        **visual_failure,
                    )
                    rejection = {
                        **visual_failure,
                        "planned_content_description": slot[
                            "content_description"
                        ],
                        "visible_description": candidate["description"],
                        "visual_slot_relevance_likert": candidate.get(
                            "visual_slot_relevance_likert"
                        ),
                    }
                    rejection_history.append(rejection)
                    active_rejections.append(rejection)

                for candidate in round_pool[slot_id]:
                    visibility_likert = int(
                        candidate["protagonist_visibility_likert"]
                    )
                    relevance_likert = int(
                        candidate["visual_slot_relevance_likert"]
                    )
                    visibility_ok = (
                        not requires_subject
                        or visibility_likert
                        >= retrieval_config.protagonist_visibility_likert_threshold
                    )
                    relevance_ok = relevance_likert >= relevance_threshold
                    if not visibility_ok:
                        reject_candidate(
                            candidate,
                            PromptFailureCode.REQUIRED_SUBJECT_NOT_VISUALLY_CONFIRMED,
                        )
                        continue
                    if not relevance_ok:
                        reject_candidate(
                            candidate,
                            PromptFailureCode.VISUAL_SLOT_NOT_RELEVANT,
                        )
                        continue
                    candidate["selection_score"] = round(
                        score_unary_candidate(slot, candidate),
                        6,
                    )
                    grounded_candidates.append(candidate)
                ranked_grounded_candidates = sorted(
                    grounded_candidates,
                    key=lambda candidate: (
                        -float(candidate["selection_score"]),
                        parse_range(candidate["timestamp"])[0],
                    ),
                )
                candidates_needed = max(
                    0,
                    retrieval_config.candidates_per_slot - len(pool[slot_id]),
                )
                selected_candidates = ranked_grounded_candidates[:candidates_needed]
                pool[slot_id].extend(selected_candidates)
                if len(ranked_grounded_candidates) > len(selected_candidates):
                    log_event(
                        "INFO",
                        "aster.timeline",
                        "stage.complete",
                        "Ranked surplus grounded candidates and retained the best-scoring set",
                        round=round_index,
                        scope=scope,
                        scope_round=scope_round,
                        slot_id=slot_id,
                        grounded_candidates=len(ranked_grounded_candidates),
                        selected_candidates=len(selected_candidates),
                        selected_candidate_ids=[
                            candidate["candidate_id"]
                            for candidate in selected_candidates
                        ],
                    )

        # A non-empty pool is already a usable binding, even when it has fewer
        # than the preferred candidate count.  Empty visual results are only a
        # repair failure once all configured base scopes have been consumed.
        # A duration-invalid binding is the exception: repeating retrieval for
        # the same too-short Segment cannot produce evidence, so it may repair
        # immediately while later base rounds remain available for refilling.
        targeted_failures = [
            failure
            for failure in duration_failures
            if not pool[str(failure["slot_id"])]
        ]
        base_scopes_exhausted = (
            not is_repair_validation
            and configured_round_index == total_rounds
        )
        if is_repair_validation or base_scopes_exhausted:
            failed_slot_ids_this_round = {
                str(failure["slot_id"])
                for failure in targeted_failures
            }
            for slot in slots:
                slot_id = str(slot["slot_id"])
                if (
                    slot_id in fixed_slot_ids
                    or pool[slot_id]
                    or slot_id in failed_slot_ids_this_round
                ):
                    continue
                slot_rejections = [
                    item
                    for item in active_rejections
                    if item["slot_id"] == slot_id
                ]
                targeted_failures.append(
                    build_prompt_failure(
                        PromptFailureCode.NO_CANDIDATE_PASSED_VISUAL_DIAGNOSTICS,
                        slot_id=slot_id,
                        round=round_index,
                        scope=scope,
                        scope_round=scope_round,
                        source_segment_ids=list(slot["source_segment_ids"]),
                        planned_content_description=slot["content_description"],
                        required_visible_subjects=list(
                            slot.get("required_visible_subjects") or []
                        ),
                        candidate_rejections=slot_rejections,
                    )
                )

        slot_by_id = {str(slot["slot_id"]): slot for slot in slots}
        for failure in targeted_failures:
            failed_slot_id = str(failure["slot_id"])
            record_failed_slot_diagnostic(
                slot_by_id[failed_slot_id],
                failure,
                missing_candidates=(
                    retrieval_config.candidates_per_slot
                    - len(pool[failed_slot_id])
                ),
            )
            failure["failed_source_segment_ids"] = sorted(
                {
                    str(segment_id)
                    for diagnostic in failed_slot_diagnostics()
                    if str(diagnostic.get("slot_id") or "") == failed_slot_id
                    for segment_id in diagnostic.get("source_segment_ids") or []
                    if str(segment_id)
                }
            )

        forced_expansion_failures: list[dict[str, Any]] | None = None
        if is_repair_validation:
            empty_repaired_slot_ids = {
                str(slot["slot_id"])
                for slot in pending
                if not pool[str(slot["slot_id"])]
            }
            if empty_repaired_slot_ids:
                may_expand_slot_ids = set(empty_repaired_slot_ids)
                blocker_map, hard_blocked_slot_ids = (
                    _repair_blockers_for_exhausted_domain(
                        slots,
                        may_expand_slot_ids,
                        repair_validation_slot_ids,
                    )
                    if may_expand_slot_ids
                    else ({}, set(empty_repaired_slot_ids))
                )
                strictly_new_blocker_ids = {
                    blocker_id
                    for blocker_ids in blocker_map.values()
                    for blocker_id in blocker_ids
                    if blocker_id not in repair_validation_slot_ids
                }
                expanded_domain = frozenset(
                    {
                        *repair_validation_slot_ids,
                        *may_expand_slot_ids,
                        *strictly_new_blocker_ids,
                    }
                )
                if (
                    replan_slots is not None
                    and may_expand_slot_ids
                    and not hard_blocked_slot_ids
                    and strictly_new_blocker_ids
                    and expanded_domain not in expanded_repair_domains
                ):
                    forced_expansion_failures = [
                        dict(failure)
                        for failure in targeted_failures
                        if str(failure["slot_id"]) in may_expand_slot_ids
                    ]
                    if not forced_expansion_failures:
                        terminally_exhausted.update(empty_repaired_slot_ids)
                        unrepairable_diagnostics = build_prompt_failure(
                            PromptFailureCode.TARGETED_REPAIR_DOMAIN_UNREPAIRABLE,
                            failed_slot_ids=sorted(empty_repaired_slot_ids),
                            reason=(
                                "repair validation produced no candidate evidence "
                                "that can support a larger local repair"
                            ),
                            repair_slot_ids=sorted(repair_validation_slot_ids),
                            blocker_slot_ids=sorted(strictly_new_blocker_ids),
                            unavailable_source_segment_ids=sorted(
                                unavailable_source_segment_ids
                            ),
                        )
                        break
                    forced_expansion_failures[0]["blocker_slot_ids_by_slot"] = {
                        slot_id: sorted(blocker_ids)
                        for slot_id, blocker_ids in blocker_map.items()
                    }
                    expanded_repair_domains.add(expanded_domain)
                    log_event(
                        "WARNING",
                        "aster.arrangement",
                        "fallback.apply",
                        "Current repair domain has no grounded candidate; expanding "
                        "through adjacent blocking Slots",
                        round=round_index,
                        failed_slot_ids=sorted(may_expand_slot_ids),
                        blocking_slot_ids=sorted(strictly_new_blocker_ids),
                        blocker_slot_ids_by_slot={
                            slot_id: sorted(blocker_ids)
                            for slot_id, blocker_ids in blocker_map.items()
                        },
                    )
                else:
                    terminally_exhausted.update(empty_repaired_slot_ids)
                    reason = (
                        "no movable adjacent blocking Slot remains"
                        if hard_blocked_slot_ids or not strictly_new_blocker_ids
                        else "the same expanded repair domain was already validated"
                    )
                    unrepairable_diagnostics = build_prompt_failure(
                        PromptFailureCode.TARGETED_REPAIR_DOMAIN_UNREPAIRABLE,
                        failed_slot_ids=sorted(empty_repaired_slot_ids),
                        reason=reason,
                        repair_slot_ids=sorted(repair_validation_slot_ids),
                        blocker_slot_ids=sorted(strictly_new_blocker_ids),
                        unavailable_source_segment_ids=sorted(
                            unavailable_source_segment_ids
                        ),
                    )
                    log_event(
                        "WARNING",
                        "aster.timeline",
                        "validation.reject",
                        "Backend marked the targeted repair domain unrepairable; "
                        "ending this ASTER attempt",
                        round=round_index,
                        **unrepairable_diagnostics,
                    )
                    break
        eligible_targeted_failures = (
            forced_expansion_failures
            if forced_expansion_failures is not None
            else [
                failure
                for failure in targeted_failures
                if str(failure["slot_id"]) not in targeted_replan_used
                if str(failure["slot_id"]) not in terminally_exhausted
                if str(failure["slot_id"]) not in repair_validation_slot_ids
            ]
        )
        if replan_slots is not None and eligible_targeted_failures:
            target_slot_ids = {
                str(failure["slot_id"])
                for failure in eligible_targeted_failures
            }
            targeted_replan_used.update(target_slot_ids)
            original_by_id = {
                str(slot["slot_id"]): slot
                for slot in slots
            }
            log_event(
                "WARNING",
                "aster.arrangement",
                "fallback.apply",
                "Redesigning failed Slots in one targeted Arrangement Architect call",
                round=round_index,
                slot_ids=sorted(target_slot_ids),
                reasons={
                    failure["slot_id"]: failure["reason_code"]
                    for failure in eligible_targeted_failures
                },
            )
            failed_source_segment_ids_by_slot: dict[str, set[str]] = {}
            for diagnostic in failed_slot_diagnostics():
                diagnostic_slot_id = str(diagnostic.get("slot_id") or "")
                if not diagnostic_slot_id:
                    continue
                failed_source_segment_ids_by_slot.setdefault(
                    diagnostic_slot_id,
                    set(),
                ).update(
                    str(segment_id)
                    for segment_id in diagnostic.get("source_segment_ids") or []
                    if str(segment_id)
                )
            failed_source_segment_ids_by_slot_payload = {
                slot_id: sorted(segment_ids)
                for slot_id, segment_ids in sorted(
                    failed_source_segment_ids_by_slot.items()
                )
                if segment_ids
            }
            for failure in eligible_targeted_failures:
                failure["failed_source_segment_ids_by_slot"] = (
                    failed_source_segment_ids_by_slot_payload
                )
            try:
                redesigned_slots, replanned_slot_ids = replan_slots(
                    slots,
                    eligible_targeted_failures,
                )
            except TargetedRepairUnrepairableError as exc:
                redesign_failure = build_prompt_failure(
                    PromptFailureCode.TARGETED_REPAIR_DOMAIN_UNREPAIRABLE,
                    **exc.diagnostics,
                )
                redesign_failure.update(
                    {
                        "failed_slot_diagnostics": failed_slot_diagnostics(),
                        "unavailable_source_segment_ids": sorted(
                            unavailable_source_segment_ids
                        ),
                    }
                )
                context.set_artifact("retrieval_failure", redesign_failure)
                log_event(
                    "WARNING",
                    "aster.arrangement",
                    "validation.reject",
                    "Backend rejected an unrepairable local domain before model "
                    "invocation; handing evidence to the complete Arrangement",
                    **redesign_failure,
                )
                raise ValueError(str(exc)) from exc
            except Exception as exc:
                redesign_failure = build_prompt_failure(
                    PromptFailureCode.TARGETED_SLOT_REDESIGN_FAILED,
                    failed_slot_ids=sorted(target_slot_ids),
                    error_type=type(exc).__name__,
                    error_message=error_summary(exc),
                    failed_slot_diagnostics=failed_slot_diagnostics(),
                    unavailable_source_segment_ids=sorted(
                        unavailable_source_segment_ids
                    ),
                )
                context.set_artifact("retrieval_failure", redesign_failure)
                log_event(
                    "ERROR",
                    "aster.arrangement",
                    "validation.reject",
                    "Targeted Slot redesign failed; handing diagnostics to the "
                    "outer ASTER attempt",
                    **redesign_failure,
                )
                raise ValueError(
                    "Targeted Slot redesign failed: "
                    f"{error_summary(exc)}"
                ) from exc
            redesigned_by_id = {
                str(slot["slot_id"]): slot
                for slot in redesigned_slots
            }
            if set(redesigned_by_id) != set(original_by_id):
                raise ValueError(
                    "Targeted Slot redesign must preserve the complete Slot ID set"
                )
            reported_replanned_slot_ids = {
                str(slot_id)
                for slot_id in replanned_slot_ids
                if str(slot_id) in original_by_id
            }
            changed_slot_ids = {
                slot_id
                for slot_id in reported_replanned_slot_ids
                if _slot_retrieval_signature(original_by_id[slot_id])
                != _slot_retrieval_signature(redesigned_by_id[slot_id])
            }
            meaningful_target_slot_ids = changed_slot_ids & target_slot_ids
            duration_failure_slot_ids = {
                str(failure["slot_id"])
                for failure in eligible_targeted_failures
                if failure["reason_code"]
                == PromptFailureCode.SOURCE_SEGMENTS_TOO_SHORT.value
            }
            for slot_id in duration_failure_slot_ids:
                if (
                    original_by_id[slot_id].get("source_segment_ids")
                    == redesigned_by_id[slot_id].get("source_segment_ids")
                ):
                    meaningful_target_slot_ids.discard(slot_id)

            if not meaningful_target_slot_ids:
                terminally_exhausted.update(target_slot_ids)
                if forced_expansion_failures is not None:
                    unrepairable_diagnostics = build_prompt_failure(
                        PromptFailureCode.TARGETED_REPAIR_DOMAIN_UNREPAIRABLE,
                        failed_slot_ids=sorted(target_slot_ids),
                        reason=(
                            "the expanded adjacent repair made no material "
                            "change to the failed retrieval constraints"
                        ),
                        repair_slot_ids=sorted(reported_replanned_slot_ids),
                        blocker_slot_ids=sorted(
                            {
                                blocker_id
                                for failure in forced_expansion_failures
                                for blocker_ids in (
                                    failure.get("blocker_slot_ids_by_slot") or {}
                                ).values()
                                for blocker_id in blocker_ids
                            }
                        ),
                        unavailable_source_segment_ids=sorted(
                            unavailable_source_segment_ids
                        ),
                    )
                log_event(
                    "WARNING",
                    "aster.arrangement",
                    "validation.reject",
                    "Targeted Slot redesign made no material change to the failed "
                    "retrieval constraints; stopping local retries",
                    round=round_index,
                    failed_slot_ids=sorted(target_slot_ids),
                    reported_slot_ids=sorted(reported_replanned_slot_ids),
                )
                if forced_expansion_failures is not None:
                    break
                continue

            accepted_changed_slot_ids = changed_slot_ids
            # Only reported, materially changed Slots are applied.  Collateral
            # neighbours whose retrieval contract did not change keep both their
            # binding and every already grounded candidate.
            slots[:] = [
                (
                    redesigned_by_id[str(slot["slot_id"])]
                    if str(slot["slot_id"]) in accepted_changed_slot_ids
                    else original_by_id[str(slot["slot_id"])]
                )
                for slot in slots
            ]
            applied_by_id = {
                str(slot["slot_id"]): slot
                for slot in slots
            }
            for slot_id in accepted_changed_slot_ids:
                fixed_candidate = applied_by_id[slot_id].get(
                    "fixed_candidate"
                )
                pool[slot_id] = (
                    [dict(fixed_candidate)]
                    if fixed_candidate is not None
                    else []
                )
                primary_scope_exhausted.discard(slot_id)
                adjacent_scope_exhausted.discard(slot_id)
                terminally_exhausted.discard(slot_id)
            active_rejections = [
                item
                for item in active_rejections
                if item["slot_id"] not in accepted_changed_slot_ids
            ]
            fixed_slot_ids.clear()
            fixed_slot_ids.update(
                str(slot["slot_id"])
                for slot in slots
                if slot.get("fixed_candidate") is not None
            )
            repair_validation_pending.update(
                slot_id
                for slot_id in accepted_changed_slot_ids
                if slot_id not in fixed_slot_ids
                if len(pool[slot_id]) < retrieval_config.candidates_per_slot
            )
            terminally_exhausted.update(
                target_slot_ids - meaningful_target_slot_ids
            )
            log_event(
                "INFO",
                "aster.arrangement",
                "stage.complete",
                "Targeted Slot redesign completed; retrying redesigned Slots",
                round=round_index,
                failed_slot_ids=sorted(target_slot_ids),
                slot_ids=sorted(accepted_changed_slot_ids),
                validation_slot_ids=sorted(repair_validation_pending),
                preserved_slot_ids=sorted(
                    reported_replanned_slot_ids - accepted_changed_slot_ids
                ),
            )
            continue
    shortages = {
        slot_id: retrieval_config.candidates_per_slot - len(candidates)
        for slot_id, candidates in pool.items()
        if slot_id not in fixed_slot_ids
        if len(candidates) < retrieval_config.candidates_per_slot
    }
    context.set_artifact("candidate_rejections", active_rejections)
    context.set_artifact("candidate_rejection_history", rejection_history)
    if shortages:
        empty_slots = [
            slot_id
            for slot_id in shortages
            if not pool[slot_id]
        ]
        slot_by_id = {str(slot["slot_id"]): slot for slot in slots}
        for slot_id in empty_slots:
            slot = slot_by_id[slot_id]
            current_segment_ids = tuple(
                str(value) for value in slot.get("source_segment_ids") or []
            )
            if (slot_id, current_segment_ids) in (
                failed_slot_diagnostics_by_assignment
            ):
                continue
            current_rejections = [
                rejection
                for rejection in rejection_history
                if str(rejection.get("slot_id") or "") == slot_id
                if tuple(
                    str(value)
                    for value in rejection.get("source_segment_ids") or []
                )
                == current_segment_ids
            ]
            terminal_failure = build_prompt_failure(
                PromptFailureCode.NO_CANDIDATE_PASSED_VISUAL_DIAGNOSTICS,
                slot_id=slot_id,
                source_segment_ids=list(current_segment_ids),
                candidate_rejections=current_rejections,
            )
            record_failed_slot_diagnostic(
                slot,
                terminal_failure,
                missing_candidates=shortages[slot_id],
            )
        if empty_slots:
            failure_details: dict[str, Any] = {
                "shortages": shortages,
                "failed_slot_ids": empty_slots,
                "failed_slot_diagnostics": failed_slot_diagnostics(),
                "unavailable_source_segment_ids": sorted(
                    unavailable_source_segment_ids
                ),
                "planned_segment_rounds": primary_rounds,
                "adjacent_expansion_rounds": adjacent_rounds,
            }
            if unrepairable_diagnostics is not None:
                failure_details["unrepairable"] = unrepairable_diagnostics
            failure = build_prompt_failure(
                PromptFailureCode.INSUFFICIENT_VISUALLY_GROUNDED_CANDIDATES,
                **failure_details,
            )
            context.set_artifact("retrieval_failure", failure)
            raise ValueError(
                "No usable candidate remains after visual diagnostics for Slots: "
                + json.dumps(empty_slots, ensure_ascii=False)
            )
        context.set_artifact("candidate_shortages", shortages)
        context.set_artifact("retrieval_failure", None)
        log_event(
            "WARNING",
            "aster.timeline",
            "fallback.apply",
            "Candidate retrieval exhausted; continuing with smaller candidate pools",
            shortages=shortages,
            planned_segment_rounds=primary_rounds,
            adjacent_expansion_rounds=adjacent_rounds,
        )
    else:
        context.set_artifact("candidate_shortages", {})
        context.set_artifact("retrieval_failure", None)
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
    # A single overlay transition, decode glitch, or hard cut must not make an
    # otherwise frozen clip look dynamic.  At the configured sampling rate a
    # normal candidate has enough frame pairs to discard the one largest
    # instantaneous difference while retaining sustained motion.
    trimmed_values = list(values)
    if len(trimmed_values) >= 3:
        trimmed_values.remove(max(trimmed_values))
    raw = sum(trimmed_values) / len(trimmed_values)
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


class TimelineScoutAgent:
    """T agent: scout and validate candidate windows on the source timeline."""

    def __init__(
        self,
        media: SegmentMediaReader,
        config: AppConfig,
        context: WorkflowContext,
        *,
        repair_slots: Callable[
            [list[dict[str, Any]], list[dict[str, Any]]],
            tuple[list[dict[str, Any]], set[str]],
        ],
    ) -> None:
        self.media = media
        self.config = config
        self.context = context
        self.repair_slots = repair_slots

    def scout(
        self,
        slots: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        return retrieve_candidates(
            slots,
            self.media,
            self.config.llm,
            self.config.vlm,
            self.config.planners.candidate_retrieval,
            self.context,
            replan_slots=self.repair_slots,
        )


__all__ = ["TimelineScoutAgent"]
