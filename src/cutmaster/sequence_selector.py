from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from cutmaster.models import VLMConfig
from cutmaster.observability import log_event
from cutmaster.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.prompting.planner import PairwiseScoringDetails
from cutmaster.workflow_context import WorkflowContext
from cutmaster.planner_shared import _edge_contact_sheet_data_url, _normalize_likert_score
from cutmaster.progress import progress_iter
from cutmaster.timecode import parse_range

def _pair_key(previous_candidate_id: str, current_candidate_id: str) -> str:
    return f"{previous_candidate_id}->{current_candidate_id}"


def _validate_pairwise_grounding(
    parsed: dict[str, Any],
    expected_pairs: list[tuple[str, str]],
) -> dict[str, dict[str, Any]]:
    expected = {_pair_key(previous, current) for previous, current in expected_pairs}
    raw_items = parsed.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("Pairwise visual response must contain an items array")
    result: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise ValueError("Pairwise visual item must be an object")
        previous_id = str(raw.get("previous_candidate_id") or "")
        current_id = str(raw.get("current_candidate_id") or "")
        key = _pair_key(previous_id, current_id)
        if key not in expected or key in result:
            raise ValueError(f"Unexpected pairwise candidate combination: {key}")
        result[key] = {
            "previous_candidate_id": previous_id,
            "current_candidate_id": current_id,
            "visual_continuity": max(
                0.0, min(1.0, float(raw["visual_continuity"]))
            ),
            "emotional_continuity": max(
                0.0, min(1.0, float(raw["emotional_continuity"]))
            ),
            "narrative_bridge": max(
                0.0, min(1.0, float(raw["narrative_bridge"]))
            ),
            "evidence": str(raw["evidence"]).strip(),
        }
    if set(result) != expected:
        raise ValueError(
            f"Pairwise visual response omitted combinations: {sorted(expected - set(result))}"
        )
    return result


def precompute_pairwise_scores(
    video_path: Path,
    slots: list[dict[str, Any]],
    pool: dict[str, list[dict[str, Any]]],
    config: VLMConfig,
    context: WorkflowContext,
    *,
    sample_frames: int,
) -> dict[str, dict[str, Any]]:
    if len(slots) < 2:
        context.set_artifact("pairwise_scores", {})
        return {}

    edge_inputs: list[tuple[str, str, dict[str, Any]]] = []
    for index, slot in enumerate(slots):
        if index > 0:
            edge_inputs.extend(
                ("head", candidate["candidate_id"], candidate)
                for candidate in pool[slot["slot_id"]]
            )
        if index < len(slots) - 1:
            edge_inputs.extend(
                ("tail", candidate["candidate_id"], candidate)
                for candidate in pool[slot["slot_id"]]
            )

    def render_edge(item: tuple[str, str, dict[str, Any]]) -> tuple[str, str, str]:
        edge, candidate_id, candidate = item
        return (
            edge,
            candidate_id,
            _edge_contact_sheet_data_url(
                video_path,
                candidate,
                edge,
                max(2, min(4, sample_frames)),
            ),
        )

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(edge_inputs)))) as executor:
        edge_images = {
            (edge, candidate_id): image_data_url
            for edge, candidate_id, image_data_url in progress_iter(
                executor.map(
                    render_edge,
                    edge_inputs,
                ),
                total=len(edge_inputs),
                description="Pairwise edge contact sheets",
                unit="edge",
            )
        }

    boundary_jobs = list(enumerate(zip(slots[:-1], slots[1:], strict=True), 1))
    pair_count = sum(
        len(pool[previous["slot_id"]]) * len(pool[current["slot_id"]])
        for previous, current in zip(slots[:-1], slots[1:], strict=True)
    )
    worker_count = max(1, min(config.max_concurrency, len(boundary_jobs)))
    log_event(
        "INFO",
        "planner.sequence",
        "stage.progress",
        "Hard-cut candidate-pair precomputation configured",
        pairs=pair_count,
        boundaries=len(boundary_jobs),
        workers=worker_count,
    )

    def score_boundary(
        job: tuple[int, tuple[dict[str, Any], dict[str, Any]]],
    ) -> dict[str, dict[str, Any]]:
        boundary_index, (previous_slot, current_slot) = job
        log_event(
            "DEBUG",
            "planner.sequence",
            "stage.progress",
            "Pairwise visual boundary started",
            boundary=boundary_index,
            boundaries=len(boundary_jobs),
        )
        previous_candidates = pool[previous_slot["slot_id"]]
        current_candidates = pool[current_slot["slot_id"]]
        expected_pairs = [
            (previous["candidate_id"], current["candidate_id"])
            for previous in previous_candidates
            for current in current_candidates
        ]
        pair_specs = [
            {
                "previous_candidate_id": previous["candidate_id"],
                "current_candidate_id": current["candidate_id"],
                "previous_visible_content": previous["description"],
                "current_visible_content": current["description"],
            }
            for previous in previous_candidates
            for current in current_candidates
        ]
        package = prompt_registry.build(
            PromptStage.PLANNER,
            PromptTask.PAIRWISE_SCORING,
            PairwiseScoringDetails(
                operation=(
                    f"Pairwise visual continuity boundary {boundary_index}/"
                    f"{len(boundary_jobs)}"
                ),
                previous_slot=previous_slot,
                current_slot=current_slot,
                pair_specs=pair_specs,
            ),
        )
        image_labels = [
            *[
                f"{candidate['candidate_id']} TAIL"
                for candidate in previous_candidates
            ],
            *[
                f"{candidate['candidate_id']} HEAD"
                for candidate in current_candidates
            ],
        ]
        image_data_urls = [
            *[
                edge_images[("tail", candidate["candidate_id"])]
                for candidate in previous_candidates
            ],
            *[
                edge_images[("head", candidate["candidate_id"])]
                for candidate in current_candidates
            ],
        ]
        result = context.call_prompt(
            package=package,
            config=config,
            validate_business=lambda parsed: _validate_pairwise_grounding(
                parsed,
                expected_pairs,
            ),
            image_data_urls=image_data_urls,
            image_labels=image_labels,
        )
        log_event(
            "DEBUG",
            "planner.sequence",
            "stage.progress",
            "Pairwise visual boundary completed",
            boundary=boundary_index,
            boundaries=len(boundary_jobs),
        )
        return result

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        boundary_results = list(
            progress_iter(
                executor.map(score_boundary, boundary_jobs),
                total=len(boundary_jobs),
                description="Pairwise VLM scoring",
                unit="boundary",
            )
        )

    scores: dict[str, dict[str, Any]] = {}
    slots_by_id = {slot["slot_id"]: slot for slot in slots}
    candidates_by_id = {
        candidate["candidate_id"]: candidate
        for values in pool.values()
        for candidate in values
    }
    for boundary_result in boundary_results:
        for key, metrics in boundary_result.items():
            previous = candidates_by_id[metrics["previous_candidate_id"]]
            current = candidates_by_id[metrics["current_candidate_id"]]
            previous_slot = slots_by_id[previous["slot_id"]]
            current_slot = slots_by_id[current["slot_id"]]
            energy_flow = _energy_flow(previous_slot, previous, current_slot, current)
            scores[key] = {
                **metrics,
                "energy_flow": round(energy_flow, 6),
                "pairwise_score": round(
                    0.40 * float(metrics["visual_continuity"])
                    + 0.25 * float(metrics["emotional_continuity"])
                    + 0.20 * float(metrics["narrative_bridge"])
                    + 0.15 * energy_flow,
                    6,
                ),
            }
    context.set_artifact("pairwise_scores", scores)
    return scores

def _unary(slot: dict[str, Any], candidate: dict[str, Any]) -> float:
    emotion_match = 1.0 - abs(
        float(slot["target_emotional_intensity"]) - float(candidate["emotional_intensity"])
    )
    kinetic_match = 1.0 - abs(
        float(slot["target_kinetic_energy"]) - float(candidate["kinetic_energy"])
    )
    duration = parse_range(candidate["timestamp"])[1] - parse_range(candidate["timestamp"])[0]
    duration_match = min(1.0, duration / max(0.1, float(slot["planned_duration_sec"])))
    requires_subject = bool(slot.get("required_visible_subjects"))
    protagonist_visibility = (
        _normalize_likert_score(candidate["protagonist_visibility_likert"])
        if requires_subject
        else 1.0
    )
    visual_relevance = float(candidate["visual_slot_relevance"])
    return (
        0.20 * float(candidate["semantic_relevance"])
        + 0.20 * visual_relevance
        + 0.20 * protagonist_visibility
        + 0.10 * emotion_match
        + 0.10 * kinetic_match
        + 0.10 * float(candidate["salience"])
        + 0.10 * duration_match
    )


def _energy_flow(
    previous_slot: dict[str, Any],
    previous: dict[str, Any],
    slot: dict[str, Any],
    current: dict[str, Any],
) -> float:
    return max(
        0.0,
        1.0
        - abs(
            (
                float(slot["target_kinetic_energy"])
                - float(previous_slot["target_kinetic_energy"])
            )
            - (
                float(current["kinetic_energy"])
                - float(previous["kinetic_energy"])
            )
        ),
    )


def _pairwise(
    previous: dict[str, Any],
    current: dict[str, Any],
    pairwise_scores: dict[str, dict[str, Any]],
) -> float:
    _, prev_end = parse_range(previous["timestamp"])
    start, _ = parse_range(current["timestamp"])
    if start < prev_end:
        return -math.inf
    key = _pair_key(previous["candidate_id"], current["candidate_id"])
    if key not in pairwise_scores:
        raise ValueError(f"Missing precomputed pairwise score: {key}")
    return float(pairwise_scores[key]["pairwise_score"])


class NoFeasiblePathError(ValueError):
    def __init__(self, slot_id: str, diagnostics: dict[str, Any]) -> None:
        super().__init__(f"No chronological non-overlapping path remains at {slot_id}")
        self.diagnostics = diagnostics


def validate_chronological_path(
    slots: list[dict[str, Any]],
    pool: dict[str, list[dict[str, Any]]],
) -> None:
    earliest_reachable_end: float | None = None
    for index, slot in enumerate(slots):
        candidates = pool[slot["slot_id"]]
        reachable = [
            candidate
            for candidate in candidates
            if earliest_reachable_end is None
            or parse_range(candidate["timestamp"])[0] >= earliest_reachable_end
        ]
        if not reachable:
            raise NoFeasiblePathError(
                slot["slot_id"],
                {
                    "failed_slot_id": slot["slot_id"],
                    "completed_slot_count": index,
                    "candidate_ranges": {
                        value["slot_id"]: [
                            candidate["timestamp"]
                            for candidate in pool[value["slot_id"]]
                        ]
                        for value in slots
                    },
                },
            )
        earliest_reachable_end = min(
            parse_range(candidate["timestamp"])[1] for candidate in reachable
        )


def select_paths(
    slots: list[dict[str, Any]],
    pool: dict[str, list[dict[str, Any]]],
    beam_width: int,
    pairwise_scores: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    beams: list[tuple[float, list[dict[str, Any]]]] = [(0.0, [])]
    for index, slot in enumerate(slots):
        expanded: list[tuple[float, list[dict[str, Any]]]] = []
        for score, path in beams:
            for candidate in pool[slot["slot_id"]]:
                pair = (
                    _pairwise(
                        path[-1],
                        candidate,
                        pairwise_scores,
                    )
                    if path
                    else 0.0
                )
                if math.isinf(pair) and pair < 0:
                    continue
                expanded.append(
                    (
                        score + 0.60 * _unary(slot, candidate) + 0.40 * pair,
                        [*path, candidate],
                    )
                )
        if not expanded:
            raise NoFeasiblePathError(
                slot["slot_id"],
                {
                    "failed_slot_id": slot["slot_id"],
                    "completed_slot_count": index,
                    "surviving_beams_before_expansion": len(beams),
                    "candidate_ranges": {
                        value["slot_id"]: [
                            candidate["timestamp"]
                            for candidate in pool[value["slot_id"]]
                        ]
                        for value in slots
                    },
                },
            )
        beams = sorted(expanded, key=lambda item: item[0], reverse=True)[:beam_width]
    best_score, best = beams[0]
    diagnostics = {
        "beam_score": round(best_score, 6),
        "beam_width": beam_width,
        "pairwise_scoring": "precomputed_vlm_hard_cut",
        "pairwise_score_count": len(pairwise_scores),
        "beam_candidate_ids": [item["candidate_id"] for item in best],
    }
    return best, diagnostics


def _score_candidate_path(
    slots: list[dict[str, Any]],
    path: list[dict[str, Any]],
    pairwise_scores: dict[str, dict[str, Any]],
) -> float:
    score = 0.0
    for index, (slot, candidate) in enumerate(zip(slots, path, strict=True)):
        score += 0.60 * _unary(slot, candidate)
        if index:
            pair = _pairwise(
                path[index - 1],
                candidate,
                pairwise_scores,
            )
            if math.isinf(pair) and pair < 0:
                return -math.inf
            score += 0.40 * pair
    return score


def path_to_script(
    slots: list[dict[str, Any]],
    path: list[dict[str, Any]],
    video_path: Path,
) -> list[dict[str, Any]]:
    script: list[dict[str, Any]] = []
    for index, (slot, candidate) in enumerate(zip(slots, path, strict=True), 1):
        output_start = float(slot["output_start_sec"])
        output_end = float(slot["output_end_sec"])
        script.append(
            {
                "_id": index,
                "video_id": 1,
                "video_name": video_path.name,
                "timestamp": candidate["timestamp"],
                "picture": slot["content_description"],
                "narration": f"播放原片{index}",
                "OST": 1,
                "slot_id": slot["slot_id"],
                "candidate_id": candidate["candidate_id"],
                "output_start_sec": output_start,
                "output_end_sec": output_end,
                "planned_duration_sec": slot["planned_duration_sec"],
                "selection_scores": {
                    "unary": round(_unary(slot, candidate), 6),
                    "semantic_relevance": candidate["semantic_relevance"],
                    "visual_slot_relevance": candidate["visual_slot_relevance"],
                    "protagonist_visibility_likert": candidate[
                        "protagonist_visibility_likert"
                    ],
                    "protagonist_visibility": _normalize_likert_score(
                        candidate["protagonist_visibility_likert"]
                    ),
                    "emotional_intensity": candidate["emotional_intensity"],
                    "kinetic_energy": candidate["kinetic_energy"],
                    "salience": candidate["salience"],
                },
            }
        )
    return script
