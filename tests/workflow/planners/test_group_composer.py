from __future__ import annotations

from pathlib import Path

import pytest

from cutmaster.workflow.planners.edit_composer import (
    EditComposerAgent,
    _future_feasible_trajectory_ids,
    _globally_viable_trajectory_ids,
    _trajectory_units,
    flatten_trajectory_path,
    path_to_script,
    validate_chronological_path,
    validate_script_against_selection,
    validate_selected_trajectory_path,
)
from cutmaster.workflow.planners.tools.errors import NoFeasiblePathError
from cutmaster.workflow.shared.execution_context import WorkflowContext


def _slot(slot_id: str, group_id: str, segment: str) -> dict:
    return {
        "slot_id": slot_id,
        "group_id": group_id,
        "planning_segment_id": f"{segment}_01",
        "source_segment_id": segment,
        "planned_duration_ms": 2000,
        "target_emotional_intensity": 0.5,
        "target_kinetic_energy": 0.5,
    }


def _trajectory(
    group_id: str,
    trajectory_id: str,
    slot: dict,
    timestamp: str,
) -> dict:
    return {
        "group_id": group_id,
        "trajectory_id": trajectory_id,
        "planning_segment_id": slot["planning_segment_id"],
        "items": [
            {
                "candidate_id": f"{trajectory_id}_item_01",
                "slot_id": slot["slot_id"],
                "source_segment_id": slot["source_segment_id"],
                "timestamp": timestamp,
                "semantic_relevance": 0.8,
                "emotional_intensity": 0.5,
                "kinetic_energy": 0.5,
                "salience": 0.8,
                "protagonist_visibility_likert": 4,
                "visual_slot_relevance_likert": 4,
            }
        ],
    }


def test_future_check_removes_a_locally_valid_dead_end() -> None:
    slots = [
        _slot("slot_01", "group_001", "segment_0001"),
        _slot("slot_02", "group_002", "segment_0002"),
        _slot("slot_03", "group_003", "segment_0003"),
    ]
    pool = {
        "group_001": [
            _trajectory("group_001", "trajectory_dead", slots[0], "00:00:08,000-00:00:10,000"),
            _trajectory("group_001", "trajectory_live", slots[0], "00:00:00,000-00:00:02,000"),
        ],
        "group_002": [
            _trajectory("group_002", "trajectory_middle", slots[1], "00:00:03,000-00:00:05,000")
        ],
        "group_003": [
            _trajectory("group_003", "trajectory_final", slots[2], "00:00:06,000-00:00:08,000")
        ],
    }

    units = _trajectory_units(slots, pool)
    feasible = _future_feasible_trajectory_ids(units)

    assert feasible[0] == {"trajectory_live"}
    validate_chronological_path(slots, pool)

    viable = _globally_viable_trajectory_ids(units)
    assert viable == [
        {"trajectory_live"},
        {"trajectory_middle"},
        {"trajectory_final"},
    ]


def test_preflight_rejects_when_no_complete_future_path_exists() -> None:
    slots = [
        _slot("slot_01", "group_001", "segment_0001"),
        _slot("slot_02", "group_002", "segment_0002"),
    ]
    pool = {
        "group_001": [
            _trajectory("group_001", "trajectory_dead", slots[0], "00:00:08,000-00:00:10,000")
        ],
        "group_002": [
            _trajectory("group_002", "trajectory_early", slots[1], "00:00:03,000-00:00:05,000")
        ],
    }

    with pytest.raises(NoFeasiblePathError) as error:
        validate_chronological_path(slots, pool)

    assert error.value.diagnostics["failed_group_id"] == "group_001"
    assert error.value.diagnostics["reason"] == "no_complete_future_path"


def test_trajectory_contract_checks_exact_slot_duration() -> None:
    slot = _slot("slot_01", "group_001", "segment_0001")
    trajectory = _trajectory(
        "group_001",
        "trajectory_001",
        slot,
        "00:00:00,000-00:00:01,999",
    )

    with pytest.raises(ValueError, match="duration does not match"):
        _trajectory_units([slot], {"group_001": [trajectory]})


def test_preflight_rechecks_planning_segment_boundaries() -> None:
    slot = _slot("slot_01", "group_001", "segment_0001")
    trajectory = _trajectory(
        "group_001",
        "trajectory_001",
        slot,
        "00:00:08,000-00:00:10,000",
    )
    planning_segments = [
        {
            "planning_segment_id": slot["planning_segment_id"],
            "source_segment_id": slot["source_segment_id"],
            "start_ms": 0,
            "end_ms": 9000,
        }
    ]

    with pytest.raises(ValueError, match="exceeds its Planning Segment"):
        validate_chronological_path(
            [slot],
            {"group_001": [trajectory]},
            planning_segments,
        )


def test_persisted_beam_path_must_be_unchanged_and_match_selection() -> None:
    slot = _slot("slot_01", "group_001", "segment_0001")
    trajectory = _trajectory(
        "group_001",
        "trajectory_001",
        slot,
        "00:00:00,000-00:00:02,000",
    )
    pool = {"group_001": [trajectory]}
    segments = [
        {
            "planning_segment_id": slot["planning_segment_id"],
            "source_segment_id": slot["source_segment_id"],
            "start_ms": 0,
            "end_ms": 2000,
        }
    ]

    validate_selected_trajectory_path(
        [slot],
        pool,
        [trajectory],
        {"group_001": "trajectory_001"},
        segments,
    )
    changed = [{**trajectory, "planning_segment_id": "changed"}]
    with pytest.raises(ValueError, match="not unchanged in its pool"):
        validate_selected_trajectory_path(
            [slot],
            pool,
            changed,
            {"group_001": "trajectory_001"},
            segments,
        )


def test_persisted_beam_path_requires_its_pairwise_scores(tmp_path: Path) -> None:
    slots = [
        _slot("slot_01", "group_001", "segment_0001"),
        _slot("slot_02", "group_002", "segment_0002"),
    ]
    path = [
        _trajectory(
            "group_001",
            "trajectory_001",
            slots[0],
            "00:00:00,000-00:00:02,000",
        ),
        _trajectory(
            "group_002",
            "trajectory_002",
            slots[1],
            "00:00:02,000-00:00:04,000",
        ),
    ]
    pool = {
        "group_001": [path[0]],
        "group_002": [path[1]],
    }
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact(
        "planning_segments",
        [
            {
                "planning_segment_id": slot["planning_segment_id"],
                "source_segment_id": slot["source_segment_id"],
                "start_ms": index * 2000,
                "end_ms": (index + 1) * 2000,
            }
            for index, slot in enumerate(slots)
        ],
    )
    agent = object.__new__(EditComposerAgent)
    agent.context = context

    with pytest.raises(ValueError, match="incomplete pairwise scores"):
        agent.validate_selected_path(
            slots,
            pool,
            path,
            {
                "group_001": "trajectory_001",
                "group_002": "trajectory_002",
            },
            {},
        )


def test_flattening_keeps_one_indivisible_trajectory_identity() -> None:
    trajectory = {
        "group_id": "group_001",
        "trajectory_id": "group_001_trajectory_01",
        "items": [
            {"slot_id": "slot_01", "candidate_id": "candidate_01"},
            {"slot_id": "slot_02", "candidate_id": "candidate_02"},
        ],
    }

    flattened = flatten_trajectory_path([trajectory])

    assert [item["slot_id"] for item in flattened] == ["slot_01", "slot_02"]
    assert {item["group_id"] for item in flattened} == {"group_001"}
    assert {item["trajectory_id"] for item in flattened} == {
        "group_001_trajectory_01"
    }


def test_revision_script_must_match_selected_groups_including_anchor() -> None:
    ordinary = {
        **_slot("slot_01", "group_001", "segment_0001"),
        "output_start_sec": 0.0,
        "output_end_sec": 2.0,
    }
    anchor = {
        **_slot("slot_02", "group_anchor", "segment_0002"),
        "planning_segment_id": None,
        "output_start_sec": 2.0,
        "output_end_sec": 4.0,
        "fixed_candidate": {
            "slot_id": "slot_02",
            "candidate_id": "anchor_candidate",
            "source_segment_id": "segment_0002",
            "timestamp": "00:00:02,000-00:00:04,000",
            "dialogue_anchor": {"dialogue_id": "dialogue_01"},
        },
    }
    trajectory = _trajectory(
        "group_001",
        "trajectory_001",
        ordinary,
        "00:00:00,000-00:00:02,000",
    )
    pool = {"group_001": [trajectory]}
    segments = [
        {
            "planning_segment_id": ordinary["planning_segment_id"],
            "source_segment_id": ordinary["source_segment_id"],
            "start_ms": 0,
            "end_ms": 2000,
        }
    ]
    units = _trajectory_units([ordinary, anchor], pool, segments)
    candidates = flatten_trajectory_path(
        [unit["trajectories"][0] for unit in units]
    )
    script = []
    for slot, candidate in zip([ordinary, anchor], candidates, strict=True):
        item = {
            "slot_id": slot["slot_id"],
            "group_id": candidate["group_id"],
            "trajectory_id": candidate["trajectory_id"],
            "candidate_id": candidate["candidate_id"],
            "timestamp": candidate["timestamp"],
            "planned_duration_ms": 2000,
            "planned_duration_sec": 2.0,
            "output_start_sec": slot["output_start_sec"],
            "output_end_sec": slot["output_end_sec"],
        }
        if candidate.get("dialogue_anchor") is not None:
            item["dialogue_anchor"] = candidate["dialogue_anchor"]
        script.append(item)

    selected = {
        "group_001": "trajectory_001",
        "group_anchor": "group_anchor_fixed",
    }
    validate_script_against_selection(
        [ordinary, anchor],
        pool,
        script,
        selected,
        segments,
    )
    changed = [dict(item) for item in script]
    changed[1]["timestamp"] = "00:00:02,100-00:00:04,100"
    with pytest.raises(ValueError, match="drifted from its selection"):
        validate_script_against_selection(
            [ordinary, anchor],
            pool,
            changed,
            selected,
            segments,
        )


def test_script_keeps_authoritative_planned_duration_ms() -> None:
    slot = {
        **_slot("slot_01", "group_001", "segment_0001"),
        "content_description": "Opening beat",
        "output_start_sec": 0.0,
        "output_end_sec": 2.0,
        "planned_duration_sec": 2.0,
        "target_emotional_intensity": 0.5,
        "target_kinetic_energy": 0.5,
    }
    candidate = {
        "candidate_id": "candidate_01",
        "group_id": "group_001",
        "trajectory_id": "trajectory_01",
        "timestamp": "00:00:00,000-00:00:02,000",
        "semantic_relevance": 0.8,
        "visual_slot_relevance_likert": 4,
        "protagonist_visibility_likert": 4,
        "emotional_intensity": 0.5,
        "kinetic_energy": 0.5,
        "salience": 0.7,
    }

    script = path_to_script([slot], [candidate], Path("video.mp4"))

    assert script[0]["planned_duration_ms"] == 2000
