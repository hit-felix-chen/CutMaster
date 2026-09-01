from __future__ import annotations

from types import SimpleNamespace

import pytest

from cutmaster.workflow.planners.aster_team import ASTERTeam
from cutmaster.workflow.shared.execution_context import WorkflowContext


def _slot(index: int) -> dict:
    return {
        "slot_id": f"slot_{index:02d}",
        "group_id": f"group_{index:03d}",
        "parent_group_id": f"group_{index:03d}",
        "source_segment_id": f"segment_{index:04d}",
        "planning_segment_id": f"segment_{index:04d}_01",
        "content_description": f"event {index}",
        "required_visible_subjects": [f"subject {index}"],
        "planned_duration_ms": 1000,
        "planned_duration_sec": 1.0,
        "output_start_ms": (index - 1) * 1000,
        "output_end_ms": index * 1000,
    }


def _planning_group(index: int) -> dict:
    return {
        "group_id": f"group_{index:03d}",
        "parent_group_id": f"group_{index:03d}",
        "source_segment_id": f"segment_{index:04d}",
        "planning_segment_id": f"segment_{index:04d}_01",
        "slot_ids": [f"slot_{index:02d}"],
    }


def _planning_segment(index: int) -> dict:
    start_ms = (index - 1) * 10_000
    return {
        "planning_segment_id": f"segment_{index:04d}_01",
        "source_segment_id": f"segment_{index:04d}",
        "start_ms": start_ms,
        "end_ms": start_ms + 5000,
    }


def _trajectory(index: int, *, suffix: str) -> dict:
    start_sec = (index - 1) * 10
    return {
        "trajectory_id": f"group_{index:03d}_trajectory_{suffix}",
        "group_id": f"group_{index:03d}",
        "planning_segment_id": f"segment_{index:04d}_01",
        "items": [
            {
                "slot_id": f"slot_{index:02d}",
                "candidate_id": f"slot_{index:02d}_candidate_{suffix}",
                "source_segment_id": f"segment_{index:04d}",
                "timestamp": (
                    f"00:00:{start_sec:02d},000-"
                    f"00:00:{start_sec + 1:02d},000"
                ),
            }
        ],
    }


def test_partial_replan_reuses_exact_candidates_even_for_affected_parent(
    tmp_path,
) -> None:
    slots = [_slot(1), _slot(2)]
    planning_groups = [_planning_group(1), _planning_group(2)]
    planning_segments = [_planning_segment(1), _planning_segment(2)]
    old_pool = {
        "group_001": [_trajectory(1, suffix="old")],
        "group_002": [_trajectory(2, suffix="old")],
    }
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("planning_groups", planning_groups)
    context.set_artifact("planning_segments", planning_segments)

    class Scout:
        def scout(self, *_args, **_kwargs):
            pytest.fail("an unchanged complete contract must be reused")

    team = ASTERTeam.__new__(ASTERTeam)
    team.context = context
    team.timeline_scout = Scout()
    team.config = SimpleNamespace(
        planners=SimpleNamespace(
            candidate_retrieval=SimpleNamespace(
                target_trajectories_per_group=2
            )
        )
    )
    team.validate_planning = lambda _slots: None

    result = team.scout_with_reuse(
        slots,
        previous_candidate_pool=old_pool,
        previous_slots=slots,
        previous_planning_groups=planning_groups,
        previous_planning_segments=planning_segments,
        affected_parent_group_ids={"group_002"},
    )

    assert result == old_pool
    assert context.get_artifact("planning_groups") == planning_groups
    assert context.get_artifact("planning_segments") == planning_segments
    assert context.get_artifact("candidate_pool") == result


def test_candidate_reuse_rejects_changed_slot_contract(tmp_path) -> None:
    slots = [_slot(1)]
    old_slots = [_slot(1)]
    old_slots[0]["content_description"] = "old event"
    planning_groups = [_planning_group(1)]
    planning_segments = [_planning_segment(1)]
    new_trajectory = _trajectory(1, suffix="new")
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("planning_groups", planning_groups)
    context.set_artifact("planning_segments", planning_segments)

    class Scout:
        def scout(
            self,
            received_slots,
            _cancellation_token=None,
            *,
            target_group_ids=None,
            seed_candidate_pool=None,
        ):
            assert received_slots == slots
            assert target_group_ids == {"group_001"}
            assert seed_candidate_pool == {}
            return {"group_001": [new_trajectory]}

    team = ASTERTeam.__new__(ASTERTeam)
    team.context = context
    team.timeline_scout = Scout()
    team.config = SimpleNamespace(
        planners=SimpleNamespace(
            candidate_retrieval=SimpleNamespace(
                target_trajectories_per_group=2
            )
        )
    )
    team.validate_planning = lambda _slots: None

    result = team.scout_with_reuse(
        slots,
        previous_candidate_pool={"group_001": [_trajectory(1, suffix="old")]},
        previous_slots=old_slots,
        previous_planning_groups=planning_groups,
        previous_planning_segments=planning_segments,
        affected_parent_group_ids=set(),
    )

    assert result == {"group_001": [new_trajectory]}


def test_partial_retrieval_failure_does_not_restore_changed_group_candidates(
    tmp_path,
) -> None:
    slots = [_slot(1), _slot(2)]
    old_slots = [_slot(1), _slot(2)]
    old_slots[1]["content_description"] = "old event 2"
    planning_groups = [_planning_group(1), _planning_group(2)]
    planning_segments = [_planning_segment(1), _planning_segment(2)]
    old_pool = {
        "group_001": [_trajectory(1, suffix="old")],
        "group_002": [_trajectory(2, suffix="old")],
    }
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("planning_groups", planning_groups)
    context.set_artifact("planning_segments", planning_segments)
    context.set_artifact("candidate_pool", old_pool)

    class Scout:
        def scout(
            self,
            received_slots,
            _cancellation_token=None,
            *,
            target_group_ids=None,
            seed_candidate_pool=None,
        ):
            assert received_slots == slots
            assert target_group_ids == {"group_002"}
            assert seed_candidate_pool == {"group_001": old_pool["group_001"]}
            assert context.get_artifact("candidate_pool") == {
                "group_001": old_pool["group_001"],
                "group_002": [],
            }
            raise RuntimeError("failed before writing partial candidates")

    team = ASTERTeam.__new__(ASTERTeam)
    team.context = context
    team.timeline_scout = Scout()
    team.config = SimpleNamespace(
        planners=SimpleNamespace(
            candidate_retrieval=SimpleNamespace(
                target_trajectories_per_group=2
            )
        )
    )
    team.validate_planning = lambda _slots: None

    with pytest.raises(RuntimeError, match="before writing"):
        team.scout_with_reuse(
            slots,
            previous_candidate_pool=old_pool,
            previous_slots=old_slots,
            previous_planning_groups=planning_groups,
            previous_planning_segments=planning_segments,
            affected_parent_group_ids={"group_002"},
        )

    assert context.get_artifact("candidate_pool") == {
        "group_001": old_pool["group_001"],
        "group_002": [],
    }


def test_partial_retrieval_keeps_anchor_between_changed_child_groups(
    tmp_path,
) -> None:
    before = {
        **_slot(1),
        "group_id": "group_001_01",
        "parent_group_id": "group_001",
        "source_segment_id": "segment_0001",
        "planning_segment_id": "segment_0001_01",
    }
    anchor = {
        **_slot(2),
        "group_id": "group_001_anchor_01",
        "parent_group_id": "group_001",
        "source_segment_id": "segment_0001",
        "fixed_candidate": {
            "candidate_id": "slot_02_dialogue_anchor",
            "slot_id": "slot_02",
            "source_segment_id": "segment_0001",
            "timestamp": "00:00:05,000-00:00:06,000",
        },
    }
    anchor.pop("planning_segment_id")
    after = {
        **_slot(3),
        "group_id": "group_001_02",
        "parent_group_id": "group_001",
        "source_segment_id": "segment_0001",
        "planning_segment_id": "segment_0001_02",
    }
    slots = [before, anchor, after]
    planning_groups = [
        {
            "group_id": "group_001_01",
            "parent_group_id": "group_001",
            "source_segment_id": "segment_0001",
            "planning_segment_id": "segment_0001_01",
            "slot_ids": ["slot_01"],
        },
        {
            "group_id": "group_001_02",
            "parent_group_id": "group_001",
            "source_segment_id": "segment_0001",
            "planning_segment_id": "segment_0001_02",
            "slot_ids": ["slot_03"],
        },
    ]
    planning_segments = [
        {
            "planning_segment_id": "segment_0001_01",
            "source_segment_id": "segment_0001",
            "start_ms": 0,
            "end_ms": 5000,
        },
        {
            "planning_segment_id": "segment_0001_02",
            "source_segment_id": "segment_0001",
            "start_ms": 6000,
            "end_ms": 10000,
        },
    ]
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("planning_groups", planning_groups)
    context.set_artifact("planning_segments", planning_segments)

    class Scout:
        def scout(
            self,
            received_slots,
            _cancellation_token=None,
            *,
            target_group_ids=None,
            seed_candidate_pool=None,
        ):
            assert [slot["slot_id"] for slot in received_slots] == [
                "slot_01",
                "slot_02",
                "slot_03",
            ]
            assert target_group_ids == {"group_001_01", "group_001_02"}
            assert seed_candidate_pool == {}
            raise RuntimeError("anchor boundary preserved")

    team = ASTERTeam.__new__(ASTERTeam)
    team.context = context
    team.timeline_scout = Scout()
    team.config = SimpleNamespace(
        planners=SimpleNamespace(
            candidate_retrieval=SimpleNamespace(
                target_trajectories_per_group=2
            )
        )
    )
    team.validate_planning = lambda _slots: None

    with pytest.raises(RuntimeError, match="anchor boundary preserved"):
        team.scout_with_reuse(
            slots,
            previous_candidate_pool={
                "group_001_01": [],
                "group_001_02": [],
            },
            previous_slots=slots,
            previous_planning_groups=planning_groups,
            previous_planning_segments=planning_segments,
            affected_parent_group_ids={"group_001"},
        )
