from cutmaster.workflow.planners.tools.planners_feedback import (
    build_stage_failure_diagnostics,
    merge_planners_feedback,
)


def test_planners_feedback_accumulates_group_failures() -> None:
    first = merge_planners_feedback(
        None,
        attempt=1,
        error="first failure",
        diagnostics={"shortages": {"slot_01": 3}},
        failed_slots=[
            {
                "slot_id": "slot_01",
                "group_id": "group_001",
                "source_segment_id": "segment_0001",
                "valid_trajectory_count": 0,
            }
        ],
        target_trajectories_per_group=3,
    )
    second = merge_planners_feedback(
        first,
        attempt=2,
        error="second failure",
        diagnostics={"shortages": {"slot_06": 1}},
        failed_slots=[
            {
                "slot_id": "slot_06",
                "group_id": "group_007",
                "source_segment_id": "segment_0007",
                "valid_trajectory_count": 1,
            }
        ],
        target_trajectories_per_group=3,
    )

    assert {item["group_id"] for item in second["failed_slots"]} == {
        "group_001",
        "group_007",
    }
    assert {item["source_segment_id"] for item in second["failed_slots"]} == {
        "segment_0001",
        "segment_0007",
    }
    assert second["target_trajectories_per_group"] == 3
    assert [item["attempt"] for item in second["failure_history"]] == [1, 2]


def test_anchor_capacity_failure_gets_group_and_repair_requirement() -> None:
    error = ValueError(
        "Anchor partition leaves an infeasible child group: "
        "failed_group_id=group_010_02 available_ms=837 required_ms=3933"
    )
    diagnostics = build_stage_failure_diagnostics(
        stage="dialogue_anchor_selection",
        error=error,
        slots=[
            {
                "slot_id": "slot_10",
                "group_id": "group_010_02",
                "parent_group_id": "group_010",
            }
        ],
    )

    assert diagnostics["reason_code"] == "anchor_partition_capacity"
    assert diagnostics["failed_parent_group_ids"] == ["group_010"]
    assert "roomier Segment" in diagnostics["repair_requirement"]
