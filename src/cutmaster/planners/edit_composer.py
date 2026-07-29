from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import AppConfig, VLMConfig
from cutmaster.runtime.observability import log_event
from cutmaster.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.prompting.planners import PairwiseScoringDetails
from cutmaster.runtime.workflow_context import WorkflowContext
from cutmaster.planners.tools.visual_scoring import _edge_contact_sheet_data_url, _normalize_likert_score
from cutmaster.planners.tools.segment_media import SegmentMediaReader
from cutmaster.planners.tools.errors import NoFeasiblePathError
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


def _score_unary_candidates(
    slot: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, float]:
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(candidates)))) as executor:
        values = executor.map(lambda candidate: _unary(slot, candidate), candidates)
        return {
            candidate["candidate_id"]: score
            for candidate, score in zip(candidates, values, strict=True)
        }


def _score_pairwise_layer(
    media: SegmentMediaReader,
    previous_slot: dict[str, Any],
    current_slot: dict[str, Any],
    previous_candidates: list[dict[str, Any]],
    current_candidates: list[dict[str, Any]],
    config: VLMConfig,
    context: WorkflowContext,
    *,
    sample_frames: int,
) -> dict[str, dict[str, Any]]:
    feasible_by_previous = {
        previous["candidate_id"]: [
            current
            for current in current_candidates
            if parse_range(current["timestamp"])[0]
            >= parse_range(previous["timestamp"])[1]
        ]
        for previous in previous_candidates
    }
    feasible_by_previous = {
        previous_id: candidates
        for previous_id, candidates in feasible_by_previous.items()
        if candidates
    }
    if not feasible_by_previous:
        return {}

    previous_by_id = {
        candidate["candidate_id"]: candidate for candidate in previous_candidates
    }
    reachable_current_ids = {
        candidate["candidate_id"]
        for candidates in feasible_by_previous.values()
        for candidate in candidates
    }
    edge_inputs = [
        *[
            ("tail", previous_id, previous_by_id[previous_id])
            for previous_id in feasible_by_previous
        ],
        *[
            ("head", candidate["candidate_id"], candidate)
            for candidate in current_candidates
            if candidate["candidate_id"] in reachable_current_ids
        ],
    ]

    def render_edge(item: tuple[str, str, dict[str, Any]]) -> tuple[str, str, str]:
        edge, candidate_id, candidate = item
        return (
            edge,
            candidate_id,
            _edge_contact_sheet_data_url(
                media,
                candidate,
                edge,
                max(2, min(4, sample_frames)),
            ),
        )

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(edge_inputs)))) as executor:
        edge_images = {
            (edge, candidate_id): image_data_url
            for edge, candidate_id, image_data_url in executor.map(
                render_edge,
                edge_inputs,
            )
        }

    jobs = list(feasible_by_previous.items())
    pair_count = sum(len(candidates) for _, candidates in jobs)
    worker_count = max(1, min(config.max_concurrency, len(jobs)))
    log_event(
        "INFO",
        "aster.composition",
        "stage.progress",
        "Lazy hard-cut scoring configured for current Beam layer",
        slot_id=current_slot["slot_id"],
        pairs=pair_count,
        surviving_previous_candidates=len(jobs),
        current_candidates=len(current_candidates),
        workers=worker_count,
    )

    def score_previous_candidate(
        job: tuple[str, list[dict[str, Any]]],
    ) -> dict[str, dict[str, Any]]:
        previous_id, feasible_current = job
        previous = previous_by_id[previous_id]
        expected_pairs = [
            (previous_id, current["candidate_id"]) for current in feasible_current
        ]
        pair_specs = [
            {
                "previous_candidate_id": previous_id,
                "current_candidate_id": current["candidate_id"],
                "previous_visible_content": previous["description"],
                "current_visible_content": current["description"],
            }
            for current in feasible_current
        ]
        package = prompt_registry.build(
            PromptStage.PLANNER,
            PromptTask.PAIRWISE_SCORING,
            PairwiseScoringDetails(
                operation=(
                    f"Lazy pairwise visual scoring for {current_slot['slot_id']} "
                    f"after {previous_id}"
                ),
                previous_slot=previous_slot,
                current_slot=current_slot,
                pair_specs=pair_specs,
            ),
        )
        image_labels = [
            f"{previous_id} TAIL",
            *[
                f"{candidate['candidate_id']} HEAD"
                for candidate in feasible_current
            ],
        ]
        image_data_urls = [
            edge_images[("tail", previous_id)],
            *[
                edge_images[("head", candidate["candidate_id"])]
                for candidate in feasible_current
            ],
        ]
        return context.call_prompt(
            package=package,
            config=config,
            validate_business=lambda parsed: _validate_pairwise_grounding(
                parsed,
                expected_pairs,
            ),
            image_data_urls=image_data_urls,
            image_labels=image_labels,
        )

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        batch_results = list(executor.map(score_previous_candidate, jobs))

    scores: dict[str, dict[str, Any]] = {}
    current_by_id = {
        candidate["candidate_id"]: candidate for candidate in current_candidates
    }
    for batch_result in batch_results:
        for key, metrics in batch_result.items():
            previous = previous_by_id[metrics["previous_candidate_id"]]
            current = current_by_id[metrics["current_candidate_id"]]
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
    visual_relevance = _normalize_likert_score(
        candidate["visual_slot_relevance_likert"]
    )
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
        raise ValueError(f"Pairwise score was not reached by Beam Search: {key}")
    return float(pairwise_scores[key]["pairwise_score"])


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
    media: SegmentMediaReader,
    slots: list[dict[str, Any]],
    pool: dict[str, list[dict[str, Any]]],
    beam_width: int,
    config: VLMConfig,
    context: WorkflowContext,
    *,
    sample_frames: int,
) -> tuple[
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, dict[str, Any]],
]:
    beams: list[tuple[float, list[dict[str, Any]]]] = [(0.0, [])]
    pairwise_scores: dict[str, dict[str, Any]] = {}
    layer_diagnostics: list[dict[str, Any]] = []
    for index, slot in enumerate(slots):
        input_beam_count = len(beams)
        current_candidates = pool[slot["slot_id"]]
        previous_candidates = list(
            {
                path[-1]["candidate_id"]: path[-1]
                for _, path in beams
                if path
            }.values()
        )
        if previous_candidates:
            with ThreadPoolExecutor(max_workers=2) as executor:
                unary_future = executor.submit(
                    _score_unary_candidates,
                    slot,
                    current_candidates,
                )
                pairwise_future = executor.submit(
                    _score_pairwise_layer,
                    media,
                    slots[index - 1],
                    slot,
                    previous_candidates,
                    current_candidates,
                    config,
                    context,
                    sample_frames=sample_frames,
                )
                unary_scores = unary_future.result()
                layer_pairwise_scores = pairwise_future.result()
            pairwise_scores.update(layer_pairwise_scores)
        else:
            unary_scores = _score_unary_candidates(slot, current_candidates)
            layer_pairwise_scores = {}

        expanded: list[tuple[float, list[dict[str, Any]]]] = []
        for score, path in beams:
            for candidate in current_candidates:
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
                        score
                        + 0.60 * unary_scores[candidate["candidate_id"]]
                        + 0.40 * pair,
                        [*path, candidate],
                    )
                )
        if not expanded:
            context.set_artifact("pairwise_scores", pairwise_scores)
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
        layer_diagnostics.append(
            {
                "slot_id": slot["slot_id"],
                "input_beams": input_beam_count,
                "unique_surviving_ends": len(previous_candidates),
                "current_candidates": len(current_candidates),
                "pairwise_scores_computed": len(layer_pairwise_scores),
                "expanded_paths": len(expanded),
                "surviving_beams": len(beams),
            }
        )
        log_event(
            "INFO",
            "aster.composition",
            "stage.progress",
            "Beam layer scored and pruned",
            **layer_diagnostics[-1],
        )
    best_score, best = beams[0]
    context.set_artifact("pairwise_scores", pairwise_scores)
    diagnostics = {
        "beam_score": round(best_score, 6),
        "beam_width": beam_width,
        "pairwise_scoring": "lazy_vlm_hard_cut_from_surviving_beam_ends",
        "pairwise_score_count": len(pairwise_scores),
        "beam_candidate_ids": [item["candidate_id"] for item in best],
        "layers": layer_diagnostics,
    }
    return best, diagnostics, pairwise_scores


def _score_candidate_path(
    slots: list[dict[str, Any]],
    path: list[dict[str, Any]],
    pairwise_scores: dict[str, dict[str, Any]],
) -> float:
    score = 0.0
    for index, (slot, candidate) in enumerate(zip(slots, path, strict=True)):
        score += 0.60 * _unary(slot, candidate)
        if index:
            try:
                pair = _pairwise(
                    path[index - 1],
                    candidate,
                    pairwise_scores,
                )
            except ValueError:
                return -math.inf
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
        item = {
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
                    "visual_slot_relevance_likert": candidate[
                        "visual_slot_relevance_likert"
                    ],
                    "visual_slot_relevance": _normalize_likert_score(
                        candidate["visual_slot_relevance_likert"]
                    ),
                    "protagonist_visibility_likert": candidate[
                        "protagonist_visibility_likert"
                    ],
                    "emotional_intensity": candidate["emotional_intensity"],
                    "kinetic_energy": candidate["kinetic_energy"],
                    "salience": candidate["salience"],
                },
            }
        if candidate.get("dialogue_anchor") is not None:
            item["dialogue_anchor"] = dict(candidate["dialogue_anchor"])
        script.append(item)
    return script


class EditComposerAgent:
    """E agent: compose the globally coherent candidate sequence."""

    def __init__(
        self,
        media: SegmentMediaReader,
        video_path: Path,
        config: AppConfig,
        context: WorkflowContext,
    ) -> None:
        self.media = media
        self.video_path = video_path
        self.config = config
        self.context = context

    def validate(
        self,
        slots: list[dict[str, Any]],
        candidate_space: dict[str, list[dict[str, Any]]],
    ) -> None:
        validate_chronological_path(slots, candidate_space)

    def compose(
        self,
        slots: list[dict[str, Any]],
        candidate_space: dict[str, list[dict[str, Any]]],
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, Any],
        dict[str, dict[str, Any]],
    ]:
        return select_paths(
            self.media,
            slots,
            candidate_space,
            self.config.beam_search.beam_width,
            self.config.vlm,
            self.context,
            sample_frames=self.config.candidate_retrieval.visual_sample_frames,
        )

    def build_script(
        self,
        slots: list[dict[str, Any]],
        selected_path: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return path_to_script(slots, selected_path, self.video_path)


__all__ = ["EditComposerAgent", "NoFeasiblePathError"]
