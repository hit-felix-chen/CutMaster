from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import cv2
from loguru import logger

from cutmaster.models import CandidateRetrievalConfig, LLMConfig, VLMConfig
from cutmaster.workflow_context import WorkflowContext
from cutmaster.planner_shared import _contact_sheet_data_url, _normalize_likert_score
from cutmaster.progress import progress_bar, progress_iter
from cutmaster.timecode import format_range, parse_range

RETRIEVER_SYSTEM = (
    "You retrieve real source-video passages from a structured VideoDescription whose Segment "
    "and Shot boundaries are authoritative. Never invent timestamps, Shots, visuals, or dialogue. "
    "Return strict JSON only."
)

VISUAL_SYSTEM = (
    "You inspect source-video contact sheets for a professional video editor. "
    "Resolve the requested subject from the maintained user request and source title, then "
    "judge whether that subject and requested action are actually visible in the supplied "
    "images. Identity must come from visible facial/person evidence, never costume, gender, "
    "scene familiarity, or dialogue. ASR and slot descriptions are evaluation targets, never "
    "visual facts. "
    "Return strict JSON only."
)

def _validate_candidates(
    parsed: dict[str, Any],
    slots: list[dict[str, Any]],
    source_segments_by_slot: dict[str, list[dict[str, Any]]],
    per_slot: int,
) -> dict[str, list[dict[str, Any]]]:
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
        seen_ranges: set[str] = set()
        available_shots = [
            shot
            for segment in source_segments_by_slot[slot_id]
            for shot in segment["shots"]
        ]
        shots_by_id = {shot["shot_id"]: shot for shot in available_shots}
        shot_positions = {
            shot["shot_id"]: index for index, shot in enumerate(available_shots)
        }
        for raw in raw_items:
            if not isinstance(raw, dict):
                raise ValueError(f"Candidate for {slot_id} must be an object")
            start, end = parse_range(str(raw.get("timestamp") or ""))
            planned_duration = float(slots_by_id[slot_id]["planned_duration_sec"])
            if end - start + 1e-6 < planned_duration:
                raise ValueError(
                    f"Candidate for {slot_id} is shorter than planned_duration_sec"
                )
            normalized_range = format_range(start, end)
            if normalized_range in seen_ranges:
                raise ValueError(f"Duplicate candidate range for {slot_id}")
            seen_ranges.add(normalized_range)
            source_shot_ids = [
                str(value).strip()
                for value in raw.get("source_shot_ids") or []
                if str(value).strip()
            ]
            if not source_shot_ids or any(
                shot_id not in shots_by_id for shot_id in source_shot_ids
            ):
                raise ValueError(f"Candidate for {slot_id} references invalid source Shots")
            positions = [shot_positions[shot_id] for shot_id in source_shot_ids]
            if positions != list(range(positions[0], positions[-1] + 1)):
                raise ValueError(f"Candidate for {slot_id} must use consecutive source Shots")
            selected_shots = [shots_by_id[shot_id] for shot_id in source_shot_ids]
            if any(
                abs(
                    float(previous["time_range"]["end_sec"])
                    - float(current["time_range"]["start_sec"])
                )
                > 1e-3
                for previous, current in zip(
                    selected_shots,
                    selected_shots[1:],
                )
            ):
                raise ValueError(
                    f"Candidate for {slot_id} crosses unavailable source Shots"
                )
            first_shot = selected_shots[0]
            last_shot = selected_shots[-1]
            expected_start = float(first_shot["time_range"]["start_sec"])
            expected_end = float(last_shot["time_range"]["end_sec"])
            if abs(start - expected_start) > 1e-3 or abs(end - expected_end) > 1e-3:
                raise ValueError(
                    f"Candidate for {slot_id} must start and end on Shot boundaries"
                )
            description = str(raw.get("description") or "").strip()
            matched_dialogue = str(raw.get("matched_dialogue") or "").strip()
            if not description:
                raise ValueError(f"Candidate for {slot_id} lacks structured visual context")
            result[slot_id].append(
                {
                    "candidate_id": f"{slot_id}_candidate_{len(result[slot_id]) + 1:02d}",
                    "slot_id": slot_id,
                    "timestamp": normalized_range,
                    "source_shot_ids": source_shot_ids,
                    "structured_context": description,
                    "description": description,
                    "matched_dialogue": matched_dialogue,
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
        result[candidate_id] = {
            "description": description,
            "visible_subjects": [
                str(value).strip()
                for value in raw.get("visible_subjects") or []
                if str(value).strip()
            ],
            "protagonist_visibility_likert": int(visibility_likert),
            "visual_slot_relevance": max(
                0.0, min(1.0, float(raw["visual_slot_relevance"]))
            ),
            "visual_evidence": str(raw["visual_evidence"]).strip(),
        }
    if set(result) != expected:
        raise ValueError(f"Visual validation omitted IDs: {sorted(expected - set(result))}")
    return result

def add_visual_features(
    video_path: Path,
    slots: list[dict[str, Any]],
    pool: dict[str, list[dict[str, Any]]],
    config: VLMConfig,
    context: WorkflowContext,
    *,
    sample_frames: int,
    operation: str,
) -> None:
    candidates = [candidate for slot in slots for candidate in pool[slot["slot_id"]]]
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(candidates)))) as executor:
        candidate_image_data_urls = list(
            progress_iter(
                executor.map(
                    lambda candidate: _contact_sheet_data_url(
                        video_path, candidate, sample_frames
                    ),
                    candidates,
                ),
                total=len(candidates),
                description="Candidate contact sheets",
                unit="candidate",
            )
        )
    slots_by_id = {slot["slot_id"]: slot for slot in slots}
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
        }

    def visual_prompt(subset: list[dict[str, Any]]) -> str:
        candidate_specs = [candidate_spec(candidate) for candidate in subset]
        return f"""Inspect the attached candidate contact sheets in exactly the listed order.
Each image is visibly labeled with its candidate ID.

First resolve the requested focal subject and editorial goal from the maintained user request,
including the source video title. For a named real person or fictional character, use visual
identity knowledge appropriate to that titled source to distinguish the actual subject from
other cast members, ensemble performers, or visually similar people. Then judge each candidate
strictly from its sampled pixels. Do not use ASR, dialogue implications, or the slot description
as evidence that the target appears. The intended content and required subjects below are
evaluation targets only; a listed name does not mean that person is visible. Names separated by
"/" are aliases for one identity, not multiple people.

Identity is a hard part of relevance: a prominent different person must receive subject
visibility 1 even if their action, gender, clothing, or setting superficially fits the request.
Source knowledge may map the requested character to their performer, but it must never map a
costume, a famous sequence, or a scene role to identity. A subject being present elsewhere in a
known scene does not establish that the person sampled here is that subject.

Before returning JSON, perform an identity-evidence check for every score of 3 or higher: at least
one sampled frame must contain a sufficiently clear face or other person-specific visual evidence
that matches the requested identity. Evidence such as "signature outfit", hair color alone,
central framing, gender, expected scene, or ASR is insufficient; lower such a score to 2 or
1. If the visible person actually has a different face, score 1. Explain the decisive pixel
evidence or the uncertainty.

Score required_subject_visibility as:
- 1: visible person is a different identity from the reference;
- 2: no usable face comparison, even if a person is prominent;
- 3: possible identity match but unclear/brief/obscured;
- 4: face clearly matches the reference in a meaningful portion;
- 5: repeated, unmistakable face match to the reference and dominant visibility.

<candidates>
{json.dumps(candidate_specs, ensure_ascii=False)}
</candidates>

Return exactly one item per candidate:
{{"items":[{{
  "candidate_id":"slot_01_candidate_01",
  "visible_description":"literal people/action/setting visible across the sampled frames",
  "visible_subjects":["only confidently identified names or generic labels"],
  "required_subject_visibility":1,
  "visual_slot_relevance":0.0,
  "visual_evidence":"brief pixel-grounded reason"
}}]}}"""

    def score_subset(
        subset: list[dict[str, Any]],
        image_urls: list[str],
        suffix: str = "",
        *,
        resampled: bool = False,
    ) -> dict[str, dict[str, Any]]:
        subset_operation = operation + suffix
        try:
            return context.call_json(
                operation=subset_operation,
                prompt=visual_prompt(subset),
                config=config,
                context_keys=["request"],
                system_prompt=VISUAL_SYSTEM,
                validate=lambda parsed: _validate_visual_grounding(parsed, subset),
                image_data_urls=image_urls,
                image_labels=[candidate["candidate_id"] for candidate in subset],
            )
        except Exception as exc:
            if "data_inspection_failed" not in str(exc).lower():
                raise
            if len(subset) > 1:
                midpoint = len(subset) // 2
                logger.warning(
                    "{} was rejected by image inspection; splitting {} candidates",
                    subset_operation,
                    len(subset),
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
                logger.warning(
                    "{} was rejected by image inspection; retrying {} with one resampled frame",
                    subset_operation,
                    candidate["candidate_id"],
                )
                return score_subset(
                    subset,
                    [_contact_sheet_data_url(video_path, candidate, 1)],
                    f"{suffix} resampled",
                    resampled=True,
                )
            raise

    with progress_bar(
        total=len(candidates),
        description="Candidate VLM validation",
        unit="candidate",
    ) as progress:
        grounded = score_subset(candidates, candidate_image_data_urls)
        progress.update(len(candidates))
    for candidate in candidates:
        candidate.update(grounded[candidate["candidate_id"]])

def _retrieval_segment_context(
    video_description: dict[str, Any],
    slots: list[dict[str, Any]],
    round_index: int,
) -> dict[str, list[dict[str, Any]]]:
    segments = video_description["segments"]
    segment_positions = {
        str(segment["segment_id"]): index
        for index, segment in enumerate(segments)
    }
    radius = max(0, round_index - 1)
    result: dict[str, list[dict[str, Any]]] = {}
    for slot in slots:
        selected_positions: set[int] = set()
        if round_index >= 3:
            selected_positions.update(range(len(segments)))
        for segment_id in slot["source_segment_ids"]:
            position = segment_positions[str(segment_id)]
            selected_positions.update(
                range(
                    max(0, position - radius),
                    min(len(segments), position + radius + 1),
                )
            )
        result[slot["slot_id"]] = [
            segments[position] for position in sorted(selected_positions)
        ]
    return result


def retrieve_candidates(
    slots: list[dict[str, Any]],
    video_path: Path,
    config: LLMConfig,
    vlm_config: VLMConfig,
    retrieval_config: CandidateRetrievalConfig,
    context: WorkflowContext,
) -> dict[str, list[dict[str, Any]]]:
    pool: dict[str, list[dict[str, Any]]] = {
        slot["slot_id"]: [] for slot in slots
    }
    video_description = context.get_artifact("video_description")
    if video_description is None:
        raise RuntimeError("Video description must be available before candidate retrieval")
    rejected: list[dict[str, Any]] = []
    borderline: dict[str, list[dict[str, Any]]] = {
        slot["slot_id"]: [] for slot in slots
    }
    for round_index in range(1, retrieval_config.retrieval_max_rounds + 1):
        pending = [
            slot
            for slot in slots
            if len(pool[slot["slot_id"]]) < retrieval_config.candidates_per_slot
        ]
        if not pending:
            break
        batches = [
            pending[index : index + retrieval_config.retrieval_batch_size]
            for index in range(0, len(pending), retrieval_config.retrieval_batch_size)
        ]
        for batch_index, batch in enumerate(batches, 1):
            source_segments_by_slot = _retrieval_segment_context(
                video_description,
                batch,
                round_index,
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
                for slot in batch
            }
            prompt = f"""Retrieve exactly {retrieval_config.candidates_per_slot} distinct source
candidates for every supplied edit Slot.

Use only the supplied structured source Segments and their Shot-level visual annotations.
Every candidate must consist of one or more consecutive source_shot_ids. Its timestamp must
exactly equal the start boundary of its first Shot and the end boundary of its last Shot.
Its duration must be at least planned_duration_sec. Silent Segments are valid source material.

Prefer each Slot's source_segment_ids, preserve source chronology, and avoid every excluded
range. Candidate descriptions must summarize the supplied visual Shot descriptions. Dialogue
may support narrative meaning but must not override visible identity or action. Score semantic
relevance, emotional intensity, and editorial salience from 0 to 1.

<slots>
{json.dumps(batch, ensure_ascii=False)}
</slots>
<excluded_ranges>
{json.dumps(excluded, ensure_ascii=False)}
</excluded_ranges>
<available_source_segments_by_slot>
{json.dumps(source_segments_by_slot, ensure_ascii=False)}
</available_source_segments_by_slot>

Return:
{{"candidates":[{{"slot_id":"slot_01","items":[{{
  "timestamp":"00:00:01,000-00:00:08,000",
  "source_shot_ids":["shot_00001","shot_00002"],
  "description":"summary grounded in the supplied Shot visual descriptions",
  "matched_dialogue":"exact supplied dialogue, or empty string for silent material",
  "semantic_relevance":0.9,
  "emotional_intensity":0.7,
  "salience":0.8
}}]}}]}}"""
            batch_pool = context.call_json(
                operation=(
                    f"Candidate retrieval round {round_index} "
                    f"batch {batch_index}/{len(batches)}"
                ),
                prompt=prompt,
                config=config,
                context_keys=["request"],
                system_prompt=RETRIEVER_SYSTEM,
                validate=lambda parsed, batch=batch: _validate_candidates(
                    parsed,
                    batch,
                    source_segments_by_slot,
                    retrieval_config.candidates_per_slot,
                ),
            )
            for slot in batch:
                slot_id = slot["slot_id"]
                for item_index, candidate in enumerate(batch_pool[slot_id], 1):
                    candidate["candidate_id"] = (
                        f"{slot_id}_round_{round_index:02d}_candidate_{item_index:02d}"
                    )
            add_visual_features(
                video_path,
                batch,
                batch_pool,
                vlm_config,
                context,
                sample_frames=retrieval_config.visual_sample_frames,
                operation=(
                    f"Visual candidate validation round {round_index} "
                    f"batch {batch_index}/{len(batches)}"
                ),
            )
            for slot in batch:
                slot_id = slot["slot_id"]
                requires_subject = bool(slot.get("required_visible_subjects"))
                for candidate in batch_pool[slot_id]:
                    visibility = _normalize_likert_score(
                        candidate["protagonist_visibility_likert"]
                    )
                    duplicate = any(
                        candidate["timestamp"] == existing["timestamp"]
                        for existing in pool[slot_id]
                    )
                    visibility_ok = (
                        not requires_subject
                        or visibility >= retrieval_config.protagonist_visibility_threshold
                    )
                    if duplicate or not visibility_ok:
                        if (
                            not duplicate
                            and requires_subject
                            and visibility
                            >= retrieval_config.protagonist_visibility_fallback_threshold
                        ):
                            borderline[slot_id].append(candidate)
                        rejected.append(
                            {
                                "slot_id": slot_id,
                                "timestamp": candidate["timestamp"],
                                "candidate_id": candidate["candidate_id"],
                                "reason": (
                                    "duplicate_range"
                                    if duplicate
                                    else "required_subject_not_visually_confirmed"
                                ),
                                "protagonist_visibility_likert": candidate[
                                    "protagonist_visibility_likert"
                                ],
                                "protagonist_visibility": visibility,
                                "visual_evidence": candidate["visual_evidence"],
                            }
                        )
                        continue
                    pool[slot_id].append(candidate)
                if len(pool[slot_id]) >= retrieval_config.candidates_per_slot:
                        break
    fallbacks: list[dict[str, Any]] = []
    for slot_id, candidates in pool.items():
        if len(candidates) >= retrieval_config.candidates_per_slot:
            continue
        for candidate in sorted(
            borderline[slot_id],
            key=lambda item: (
                int(item["protagonist_visibility_likert"]),
                float(item["visual_slot_relevance"]),
                float(item["semantic_relevance"]),
            ),
            reverse=True,
        ):
            if any(
                candidate["timestamp"] == existing["timestamp"]
                for existing in candidates
            ):
                continue
            candidate["visual_validation_mode"] = "borderline_identity_fallback"
            candidates.append(candidate)
            fallbacks.append(
                {
                    "slot_id": slot_id,
                    "candidate_id": candidate["candidate_id"],
                    "timestamp": candidate["timestamp"],
                    "protagonist_visibility_likert": candidate[
                        "protagonist_visibility_likert"
                    ],
                    "protagonist_visibility": _normalize_likert_score(
                        candidate["protagonist_visibility_likert"]
                    ),
                    "reason": "exact_candidate_count_after_exhaustive_visual_retrieval",
                }
            )
            if len(candidates) >= retrieval_config.candidates_per_slot:
                break
    shortages = {
        slot_id: retrieval_config.candidates_per_slot - len(candidates)
        for slot_id, candidates in pool.items()
        if len(candidates) < retrieval_config.candidates_per_slot
    }
    context.set_artifact("candidate_rejections", rejected)
    context.set_artifact("candidate_fallbacks", fallbacks)
    if shortages:
        failure = {
            "reason": "insufficient_visually_grounded_candidates",
            "shortages": shortages,
            "rounds": retrieval_config.retrieval_max_rounds,
        }
        context.set_artifact("retrieval_failure", failure)
        raise ValueError(
            "Could not obtain required visually grounded candidates: "
            + json.dumps(shortages, ensure_ascii=False)
        )
    for slot_id, candidates in pool.items():
        candidates.sort(key=lambda item: parse_range(item["timestamp"])[0])
        for index, candidate in enumerate(candidates, 1):
            candidate["candidate_id"] = f"{slot_id}_candidate_{index:02d}"
    add_kinetic_features(
        video_path,
        pool,
        retrieval_config.motion_sample_fps,
        retrieval_config.motion_workers,
    )
    context.set_artifact("candidate_pool", pool)
    return pool


def _candidate_motion(video: cv2.VideoCapture, start: float, end: float, fps: float) -> float:
    values: list[float] = []
    previous = None
    sample_step = 1.0 / max(fps, 0.1)
    next_sample = start
    video.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
    while True:
        ok, frame = video.read()
        if not ok:
            break
        time_sec = float(video.get(cv2.CAP_PROP_POS_MSEC)) / 1000.0
        if time_sec >= end:
            break
        if time_sec + 1e-6 < next_sample:
            continue
        gray = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (160, 90))
        if previous is not None:
            values.append(float(cv2.absdiff(gray, previous).mean()) / 255.0)
        previous = gray
        next_sample += sample_step
    if not values:
        return 0.0
    raw = sum(values) / len(values)
    return max(0.0, min(1.0, raw / 0.18))


def add_kinetic_features(
    video_path: Path,
    pool: dict[str, list[dict[str, Any]]],
    sample_fps: float,
    max_workers: int = 4,
) -> None:
    candidates = [candidate for values in pool.values() for candidate in values]
    if not candidates:
        return
    worker_count = max(1, min(max_workers, len(candidates)))
    groups = [candidates[index::worker_count] for index in range(worker_count)]
    progress = progress_bar(
        total=len(candidates),
        description="Candidate motion analysis",
        unit="candidate",
    )

    def process(group: list[dict[str, Any]]) -> None:
        video = cv2.VideoCapture(str(video_path))
        if not video.isOpened():
            raise RuntimeError(f"Could not open source video: {video_path}")
        try:
            for candidate in group:
                start, end = parse_range(candidate["timestamp"])
                candidate["kinetic_energy"] = round(
                    _candidate_motion(video, start, end, sample_fps), 4
                )
                progress.update()
        finally:
            video.release()

    try:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            list(executor.map(process, groups))
    finally:
        progress.close()
