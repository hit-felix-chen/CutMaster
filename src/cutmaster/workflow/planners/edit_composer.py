from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import AppConfig, VLMConfig
from cutmaster.infrastructure.observability.logging import log_event
from cutmaster.workflow.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.workflow.prompting.planners import PairwiseScoringDetails
from cutmaster.workflow.shared.execution_context import WorkflowContext
from cutmaster.workflow.planners.tools.visual_scoring import _edge_contact_sheet_data_url, _normalize_likert_score
from cutmaster.workflow.planners.tools.segment_media import SegmentMediaReader
from cutmaster.workflow.planners.tools.errors import NoFeasiblePathError
from cutmaster.workflow.shared.timecode import parse_range

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
            PromptStage.PLANNERS,
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

def score_unary_candidate(
    slot: dict[str, Any],
    candidate: dict[str, Any],
) -> float:
    emotion_match = 1.0 - abs(
        float(slot["target_emotional_intensity"]) - float(candidate["emotional_intensity"])
    )
    kinetic_match = 1.0 - abs(
        float(slot["target_kinetic_energy"]) - float(candidate["kinetic_energy"])
    )
    duration = parse_range(candidate["timestamp"])[1] - parse_range(candidate["timestamp"])[0]
    planned_duration = int(slot["planned_duration_ms"]) / 1000.0
    duration_match = min(1.0, duration / max(0.1, planned_duration))
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


def _trajectory_units(
    slots: list[dict[str, Any]],
    pool: dict[str, list[dict[str, Any]]],
    planning_segments: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    segment_boundaries: dict[str, tuple[int, int]] | None = None
    if planning_segments is not None:
        segment_boundaries = {}
        for segment in planning_segments:
            segment_id = str(segment.get("planning_segment_id") or "")
            start_ms = segment.get("start_ms")
            end_ms = segment.get("end_ms")
            if (
                not segment_id
                or segment_id in segment_boundaries
                or isinstance(start_ms, bool)
                or not isinstance(start_ms, int)
                or isinstance(end_ms, bool)
                or not isinstance(end_ms, int)
                or end_ms <= start_ms
            ):
                raise ValueError("Invalid or duplicate Planning Segment boundary")
            segment_boundaries[segment_id] = (start_ms, end_ms)
    units: list[dict[str, Any]] = []
    closed_group_ids: set[str] = set()
    candidate_ids: set[str] = set()
    index = 0
    while index < len(slots):
        first = slots[index]
        group_id = str(first.get("group_id") or "")
        if not group_id:
            raise ValueError(f"Slot {first.get('slot_id')} has no group_id")
        if group_id in closed_group_ids:
            raise ValueError(f"Slot Group {group_id} must be one contiguous run")
        group_slots: list[dict[str, Any]] = []
        while index < len(slots) and str(slots[index].get("group_id")) == group_id:
            group_slots.append(slots[index])
            index += 1
        closed_group_ids.add(group_id)
        fixed = [slot for slot in group_slots if slot.get("fixed_candidate") is not None]
        if fixed:
            if len(group_slots) != 1 or len(fixed) != 1:
                raise ValueError(f"Anchor group {group_id} must contain exactly one Slot")
            trajectories = [
                {
                    "trajectory_id": f"{group_id}_fixed",
                    "group_id": group_id,
                    "planning_segment_id": None,
                    "items": [dict(fixed[0]["fixed_candidate"])],
                    "fixed": True,
                }
            ]
        else:
            trajectories = [dict(value) for value in pool.get(group_id, [])]
            if not trajectories:
                raise NoFeasiblePathError(
                    group_id,
                    {"failed_group_id": group_id, "reason": "empty_trajectory_pool"},
                )
        expected_slot_ids = [str(slot["slot_id"]) for slot in group_slots]
        seen_ids: set[str] = set()
        for trajectory in trajectories:
            trajectory_id = str(trajectory.get("trajectory_id") or "")
            if not trajectory_id or trajectory_id in seen_ids:
                raise ValueError(f"Invalid or duplicate trajectory_id in {group_id}")
            seen_ids.add(trajectory_id)
            if str(trajectory.get("group_id")) != group_id:
                raise ValueError(f"Trajectory {trajectory_id} belongs to another group")
            if not trajectory.get("fixed"):
                expected_planning_segment_id = str(
                    group_slots[0].get("planning_segment_id") or ""
                )
                if not expected_planning_segment_id or str(
                    trajectory.get("planning_segment_id") or ""
                ) != expected_planning_segment_id:
                    raise ValueError(
                        f"Trajectory {trajectory_id} is outside its Planning Segment"
                    )
                if (
                    segment_boundaries is not None
                    and expected_planning_segment_id not in segment_boundaries
                ):
                    raise ValueError(
                        f"Trajectory {trajectory_id} has no Planning Segment boundary"
                    )
            items = trajectory.get("items")
            if not isinstance(items, list):
                raise ValueError(f"Trajectory {trajectory_id} items must be a list")
            if [str(item.get("slot_id")) for item in items] != expected_slot_ids:
                raise ValueError(
                    f"Trajectory {trajectory_id} must cover {expected_slot_ids} in order"
                )
            previous_end: float | None = None
            for slot, item in zip(group_slots, items, strict=True):
                candidate_id = str(item.get("candidate_id") or "")
                if not candidate_id or candidate_id in candidate_ids:
                    raise ValueError(
                        f"Invalid or duplicate candidate_id in {trajectory_id}"
                    )
                candidate_ids.add(candidate_id)
                if str(item.get("source_segment_id") or "") != str(
                    slot.get("source_segment_id") or ""
                ):
                    raise ValueError(
                        f"Candidate {candidate_id} belongs to another source Segment"
                    )
                start, end = parse_range(str(item["timestamp"]))
                if segment_boundaries is not None and not trajectory.get("fixed"):
                    boundary_start_ms, boundary_end_ms = segment_boundaries[
                        expected_planning_segment_id
                    ]
                    start_ms = int(round(start * 1000.0))
                    end_ms = int(round(end * 1000.0))
                    if start_ms < boundary_start_ms or end_ms > boundary_end_ms:
                        raise ValueError(
                            f"Candidate {candidate_id} exceeds its Planning Segment"
                        )
                actual_duration_ms = int(round((end - start) * 1000.0))
                if actual_duration_ms != int(slot["planned_duration_ms"]):
                    raise ValueError(
                        f"Candidate {candidate_id} duration does not match "
                        f"{slot['slot_id']}"
                    )
                if previous_end is not None and start < previous_end:
                    raise ValueError(
                        f"Trajectory {trajectory_id} overlaps or reverses source time"
                    )
                previous_end = end
        units.append(
            {
                "group_id": group_id,
                "slots": group_slots,
                "trajectories": trajectories,
            }
        )
    if set(pool) != {
        unit["group_id"]
        for unit in units
        if not bool(unit["trajectories"][0].get("fixed"))
    }:
        raise ValueError("Candidate trajectory pool does not match the planned Slot Groups")
    return units


def validate_chronological_path(
    slots: list[dict[str, Any]],
    pool: dict[str, list[dict[str, Any]]],
    planning_segments: list[dict[str, Any]] | None = None,
) -> None:
    units = _trajectory_units(slots, pool, planning_segments)
    future_feasible = _future_feasible_trajectory_ids(units)
    for index, feasible_ids in enumerate(future_feasible):
        if not feasible_ids:
            unit = units[index]
            raise NoFeasiblePathError(
                unit["group_id"],
                {
                    "failed_group_id": unit["group_id"],
                    "reason": "no_complete_future_path",
                    "trajectory_ranges": {
                        value["group_id"]: [
                            [
                                trajectory["items"][0]["timestamp"],
                                trajectory["items"][-1]["timestamp"],
                            ]
                            for trajectory in value["trajectories"]
                        ]
                        for value in units
                    },
                },
            )


def _trajectory_source_range(
    trajectory: dict[str, Any],
) -> tuple[float, float]:
    items = trajectory["items"]
    return (
        parse_range(items[0]["timestamp"])[0],
        parse_range(items[-1]["timestamp"])[1],
    )


def _future_feasible_trajectory_ids(
    units: list[dict[str, Any]],
) -> list[set[str]]:
    """Mark trajectories that can still reach a legal complete suffix path."""

    if not units:
        return []
    feasible: list[set[str]] = [set() for _ in units]
    feasible[-1] = {
        str(trajectory["trajectory_id"])
        for trajectory in units[-1]["trajectories"]
    }
    for index in range(len(units) - 2, -1, -1):
        next_trajectories = {
            str(trajectory["trajectory_id"]): trajectory
            for trajectory in units[index + 1]["trajectories"]
            if str(trajectory["trajectory_id"]) in feasible[index + 1]
        }
        for trajectory in units[index]["trajectories"]:
            _, end = _trajectory_source_range(trajectory)
            if any(
                _trajectory_source_range(next_trajectory)[0] >= end
                for next_trajectory in next_trajectories.values()
            ):
                feasible[index].add(str(trajectory["trajectory_id"]))
    return feasible


def _globally_viable_trajectory_ids(
    units: list[dict[str, Any]],
) -> list[set[str]]:
    if not units:
        return []
    reachable: list[set[str]] = [set() for _ in units]
    reachable[0] = {
        str(trajectory["trajectory_id"])
        for trajectory in units[0]["trajectories"]
    }
    for index in range(1, len(units)):
        previous = [
            trajectory
            for trajectory in units[index - 1]["trajectories"]
            if str(trajectory["trajectory_id"]) in reachable[index - 1]
        ]
        for trajectory in units[index]["trajectories"]:
            start, _ = _trajectory_source_range(trajectory)
            if any(
                _trajectory_source_range(previous_trajectory)[1] <= start
                for previous_trajectory in previous
            ):
                reachable[index].add(str(trajectory["trajectory_id"]))
    suffix_feasible = _future_feasible_trajectory_ids(units)
    return [
        prefix_ids & suffix_ids
        for prefix_ids, suffix_ids in zip(
            reachable,
            suffix_feasible,
            strict=True,
        )
    ]


def _slot_candidates(
    slots: list[dict[str, Any]],
    units: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    result = {str(slot["slot_id"]): {} for slot in slots}
    for unit in units:
        for trajectory in unit["trajectories"]:
            for candidate in trajectory["items"]:
                result[str(candidate["slot_id"])][str(candidate["candidate_id"])] = candidate
    return {
        slot_id: list(candidates.values())
        for slot_id, candidates in result.items()
    }


def _score_all_adjacent_edges(
    media: SegmentMediaReader,
    slots: list[dict[str, Any]],
    units: list[dict[str, Any]],
    config: VLMConfig,
    context: WorkflowContext,
    *,
    sample_frames: int,
) -> dict[str, dict[str, Any]]:
    candidates = _slot_candidates(slots, units)
    scores: dict[str, dict[str, Any]] = {}
    for index in range(1, len(slots)):
        previous_slot = slots[index - 1]
        current_slot = slots[index]
        scores.update(
            _score_pairwise_layer(
                media,
                previous_slot,
                current_slot,
                candidates[str(previous_slot["slot_id"])],
                candidates[str(current_slot["slot_id"])],
                config,
                context,
                sample_frames=sample_frames,
            )
        )
    return scores


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
    units = _trajectory_units(slots, pool)
    future_feasible = _future_feasible_trajectory_ids(units)
    pairwise_scores = _score_all_adjacent_edges(
        media,
        slots,
        units,
        config,
        context,
        sample_frames=sample_frames,
    )
    slot_by_id = {str(slot["slot_id"]): slot for slot in slots}
    unary_scores = {
        str(candidate["candidate_id"]): score_unary_candidate(
            slot_by_id[str(candidate["slot_id"])],
            candidate,
        )
        for candidates in _slot_candidates(slots, units).values()
        for candidate in candidates
    }

    def trajectory_score(trajectory: dict[str, Any]) -> float:
        items = trajectory["items"]
        score = sum(0.60 * unary_scores[str(item["candidate_id"])] for item in items)
        for previous, current in zip(items, items[1:]):
            score += 0.40 * _pairwise(previous, current, pairwise_scores)
        return score

    beams: list[tuple[float, list[dict[str, Any]], list[dict[str, Any]]]] = [
        (0.0, [], [])
    ]
    layer_diagnostics: list[dict[str, Any]] = []
    for unit_index, unit in enumerate(units):
        input_beam_count = len(beams)
        expanded: list[
            tuple[float, list[dict[str, Any]], list[dict[str, Any]]]
        ] = []
        for score, path, flat_path in beams:
            for trajectory in unit["trajectories"]:
                if (
                    str(trajectory["trajectory_id"])
                    not in future_feasible[unit_index]
                ):
                    continue
                items = trajectory["items"]
                own_score = trajectory_score(trajectory)
                pair = _pairwise(flat_path[-1], items[0], pairwise_scores) if flat_path else 0.0
                if math.isinf(pair) and pair < 0:
                    continue
                expanded.append(
                    (
                        score + own_score + 0.40 * pair,
                        [*path, trajectory],
                        [*flat_path, *items],
                    )
                )
        if not expanded:
            context.set_artifact("pairwise_scores", pairwise_scores)
            raise NoFeasiblePathError(
                unit["group_id"],
                {
                    "failed_group_id": unit["group_id"],
                    "surviving_beams_before_expansion": len(beams),
                },
            )
        beams = sorted(expanded, key=lambda item: item[0], reverse=True)[:beam_width]
        layer_diagnostics.append(
            {
                "group_id": unit["group_id"],
                "input_beams": input_beam_count,
                "current_trajectories": len(unit["trajectories"]),
                "future_feasible_trajectories": len(
                    future_feasible[unit_index]
                ),
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
    best_score, best, flat_best = beams[0]
    context.set_artifact("pairwise_scores", pairwise_scores)
    diagnostics = {
        "beam_score": round(best_score, 6),
        "beam_width": beam_width,
        "pairwise_scoring": "lazy_vlm_hard_cut_from_surviving_beam_ends",
        "pairwise_score_count": len(pairwise_scores),
        "selected_trajectory_ids": {
            str(item["group_id"]): str(item["trajectory_id"])
            for item in best
        },
        "beam_candidate_ids": [item["candidate_id"] for item in flat_best],
        "layers": layer_diagnostics,
    }
    context.set_artifact(
        "selected_trajectory_ids",
        diagnostics["selected_trajectory_ids"],
    )
    return best, diagnostics, pairwise_scores


def select_first_trajectory_path(
    slots: list[dict[str, Any]],
    pool: dict[str, list[dict[str, Any]]],
    planning_segments: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, dict[str, Any]]]:
    """Select the first retained trajectory per group, with no scoring/search.

    Pool order is retrieval/validation order, not score order. Keep fixed anchors
    and whole trajectories. A conflicting first choice fails rather than silently
    searching for a better alternative, which would change the ablation.
    """
    units = _trajectory_units(slots, pool, planning_segments)
    path = [unit["trajectories"][0] for unit in units]
    previous_end = -1.0
    for trajectory in path:
        start, end = _trajectory_source_range(trajectory)
        if start < previous_end:
            raise NoFeasiblePathError(str(trajectory["group_id"]), {
                "reason": "first_trajectory_source_overlap",
                "previous_end_sec": previous_end,
                "source_start_sec": start,
            })
        previous_end = end
    return path, {
        "selection_mode": "first",
        "selection_order": "retained_retrieval_order",
        "selected_trajectory_ids": {
            str(t["group_id"]): str(t["trajectory_id"]) for t in path
        },
        "beam_candidate_ids": [c["candidate_id"] for c in flatten_trajectory_path(path)],
        "beam_width": 0,
        "pairwise_scoring": "disabled",
        "pairwise_score_count": 0,
    }, {}


def _score_candidate_path(
    slots: list[dict[str, Any]],
    path: list[dict[str, Any]],
    pairwise_scores: dict[str, dict[str, Any]],
) -> float:
    score = 0.0
    for index, (slot, candidate) in enumerate(zip(slots, path, strict=True)):
        score += 0.60 * score_unary_candidate(slot, candidate)
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
            "group_id": candidate.get("group_id", slot.get("group_id")),
            "trajectory_id": candidate.get("trajectory_id"),
            "output_start_sec": output_start,
            "output_end_sec": output_end,
            "planned_duration_ms": slot["planned_duration_ms"],
            "planned_duration_sec": slot["planned_duration_sec"],
            "selection_scores": {
                "unary": round(score_unary_candidate(slot, candidate), 6),
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
        for field in (
            "planning_segment_id",
            "planning_segment_start_ms",
            "planning_segment_end_ms",
            "source_segment_id",
        ):
            if field in candidate:
                item[field] = candidate[field]
        if candidate.get("source_shot_ids") is not None:
            item["source_shot_ids"] = list(candidate["source_shot_ids"])
        script.append(item)
    return script


def flatten_trajectory_path(
    trajectories: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for trajectory in trajectories:
        trajectory_id = str(trajectory["trajectory_id"])
        group_id = str(trajectory["group_id"])
        for raw in trajectory["items"]:
            candidate = dict(raw)
            candidate["trajectory_id"] = trajectory_id
            candidate["group_id"] = group_id
            result.append(candidate)
    return result


def validate_selected_trajectory_path(
    slots: list[dict[str, Any]],
    pool: dict[str, list[dict[str, Any]]],
    selected_path: list[dict[str, Any]],
    selected_trajectory_ids: dict[str, Any],
    planning_segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate one persisted whole-trajectory choice against its pool."""

    units = _trajectory_units(slots, pool, planning_segments)
    if len(selected_path) != len(units):
        raise ValueError("Selected trajectory path must cover every Slot Group")
    expected_selection: dict[str, str] = {}
    canonical_path: list[dict[str, Any]] = []
    for unit, selected in zip(units, selected_path, strict=True):
        if not isinstance(selected, dict):
            raise ValueError("Selected trajectory path items must be objects")
        group_id = str(selected.get("group_id") or "")
        trajectory_id = str(selected.get("trajectory_id") or "")
        if group_id != unit["group_id"] or not trajectory_id:
            raise ValueError("Selected trajectory path is not in Slot Group order")
        available = {
            str(trajectory["trajectory_id"]): trajectory
            for trajectory in unit["trajectories"]
        }
        canonical = available.get(trajectory_id)
        if canonical is None or selected != canonical:
            raise ValueError(
                f"Selected trajectory {trajectory_id} is not unchanged in its pool"
            )
        expected_selection[group_id] = trajectory_id
        canonical_path.append(canonical)
    normalized_selection = {
        str(group_id): str(trajectory_id)
        for group_id, trajectory_id in selected_trajectory_ids.items()
    }
    if normalized_selection != expected_selection:
        raise ValueError("Selected trajectory IDs disagree with the selected path")
    previous_end = -math.inf
    for trajectory in canonical_path:
        start, end = _trajectory_source_range(trajectory)
        if start < previous_end:
            raise ValueError("Selected trajectory path overlaps or reverses source time")
        previous_end = end
    return canonical_path


def validate_script_against_selection(
    slots: list[dict[str, Any]],
    pool: dict[str, list[dict[str, Any]]],
    script: list[dict[str, Any]],
    selected_trajectory_ids: dict[str, Any],
    planning_segments: list[dict[str, Any]],
    pairwise_scores: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Require a persisted revised script to equal its whole-group selection."""

    units = _trajectory_units(slots, pool, planning_segments)
    selected_path: list[dict[str, Any]] = []
    for unit in units:
        group_id = str(unit["group_id"])
        trajectory_id = str(selected_trajectory_ids.get(group_id) or "")
        matches = [
            trajectory
            for trajectory in unit["trajectories"]
            if str(trajectory["trajectory_id"]) == trajectory_id
        ]
        if len(matches) != 1:
            raise ValueError(f"Selection has no unique trajectory for {group_id}")
        selected_path.append(matches[0])
    canonical_path = validate_selected_trajectory_path(
        slots,
        pool,
        selected_path,
        selected_trajectory_ids,
        planning_segments,
    )
    candidates = flatten_trajectory_path(canonical_path)
    if pairwise_scores is not None and not math.isfinite(
        _score_candidate_path(slots, candidates, pairwise_scores)
    ):
        raise ValueError("Revised script has incomplete pairwise scores")
    if len(script) != len(slots) or len(script) != len(candidates):
        raise ValueError("Revised script must contain exactly one item per Slot")
    for slot, candidate, item in zip(slots, candidates, script, strict=True):
        if not isinstance(item, dict):
            raise ValueError("Revised script items must be objects")
        expected = {
            "slot_id": str(slot["slot_id"]),
            "group_id": str(candidate["group_id"]),
            "trajectory_id": str(candidate["trajectory_id"]),
            "candidate_id": str(candidate["candidate_id"]),
            "timestamp": str(candidate["timestamp"]),
        }
        if any(str(item.get(key) or "") != value for key, value in expected.items()):
            raise ValueError(
                f"Revised script item for {slot['slot_id']} drifted from its selection"
            )
        planned_duration_ms = slot.get("planned_duration_ms")
        if (
            not isinstance(planned_duration_ms, int)
            or isinstance(planned_duration_ms, bool)
            or item.get("planned_duration_ms") != planned_duration_ms
        ):
            raise ValueError(
                f"Revised script duration drifted for {slot['slot_id']}"
            )
        expected_duration_sec = planned_duration_ms / 1000.0
        if float(item.get("planned_duration_sec", -1.0)) != expected_duration_sec:
            raise ValueError(
                f"Revised script duration drifted for {slot['slot_id']}"
            )
        for field in ("output_start_sec", "output_end_sec"):
            if float(item.get(field, -1.0)) != float(slot[field]):
                raise ValueError(
                    f"Revised script output timing drifted for {slot['slot_id']}"
                )
        if item.get("dialogue_anchor") != candidate.get("dialogue_anchor"):
            raise ValueError(
                f"Revised script Anchor drifted for {slot['slot_id']}"
            )


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
        planning_segments = self.context.get_artifact("planning_segments")
        if not isinstance(planning_segments, list):
            raise RuntimeError("Planning Segments are required before composition")
        validate_chronological_path(
            slots,
            candidate_space,
            planning_segments,
        )

    def validate_selected_path(
        self,
        slots: list[dict[str, Any]],
        candidate_space: dict[str, list[dict[str, Any]]],
        selected_path: list[dict[str, Any]],
        selected_trajectory_ids: dict[str, Any],
        pairwise_scores: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        planning_segments = self.context.get_artifact("planning_segments")
        if not isinstance(planning_segments, list):
            raise RuntimeError("Planning Segments are required before composition")
        canonical_path = validate_selected_trajectory_path(
            slots,
            candidate_space,
            selected_path,
            selected_trajectory_ids,
            planning_segments,
        )
        if pairwise_scores is not None:
            score = _score_candidate_path(
                slots,
                flatten_trajectory_path(canonical_path),
                pairwise_scores,
            )
            if not math.isfinite(score):
                raise ValueError(
                    "Selected trajectory path has incomplete pairwise scores"
                )

    def validate_script(
        self,
        slots: list[dict[str, Any]],
        candidate_space: dict[str, list[dict[str, Any]]],
        script: list[dict[str, Any]],
        selected_trajectory_ids: dict[str, Any],
        pairwise_scores: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        planning_segments = self.context.get_artifact("planning_segments")
        if not isinstance(planning_segments, list):
            raise RuntimeError("Planning Segments are required before composition")
        validate_script_against_selection(
            slots,
            candidate_space,
            script,
            selected_trajectory_ids,
            planning_segments,
            pairwise_scores,
        )

    def compose(
        self,
        slots: list[dict[str, Any]],
        candidate_space: dict[str, list[dict[str, Any]]],
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, Any],
        dict[str, dict[str, Any]],
    ]:
        if self.config.planners.beam_search.selection_mode == "first":
            path, diagnostics, scores = select_first_trajectory_path(
                slots, candidate_space, self.context.get_artifact("planning_segments")
            )
            self.context.set_artifact("selected_trajectory_ids", diagnostics["selected_trajectory_ids"])
            self.context.set_artifact("pairwise_scores", scores)
            return path, diagnostics, scores
        return select_paths(
            self.media,
            slots,
            candidate_space,
            self.config.planners.beam_search.beam_width,
            self.config.vlm,
            self.context,
            sample_frames=self.config.planners.candidate_retrieval.visual_sample_frames,
        )

    def build_script(
        self,
        slots: list[dict[str, Any]],
        selected_path: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        return path_to_script(
            slots,
            flatten_trajectory_path(selected_path),
            self.video_path,
        )


__all__ = [
    "select_first_trajectory_path",
    "EditComposerAgent",
    "NoFeasiblePathError",
    "flatten_trajectory_path",
    "path_to_script",
    "validate_script_against_selection",
    "validate_selected_trajectory_path",
]
