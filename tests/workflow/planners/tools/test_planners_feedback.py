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
    )

    assert {item["group_id"] for item in second["failed_slots"]} == {
        "group_001",
        "group_007",
    }
    assert {item["source_segment_id"] for item in second["failed_slots"]} == {
        "segment_0001",
        "segment_0007",
    }
    assert [item["attempt"] for item in second["failure_history"]] == [1, 2]


def test_semantic_zero_candidate_does_not_forbid_the_previous_binding() -> None:
    feedback = merge_planners_feedback(
        None,
        attempt=1,
        error="semantic zero candidate",
        diagnostics={
            "semantic_zero_candidate_group_ids": ["group_001_02"],
            "failed_parent_group_ids": ["group_001"],
        },
        failed_slots=[
            {
                "slot_id": "slot_02",
                "group_id": "group_001_02",
                "parent_group_id": "group_001",
                "source_segment_id": "segment_0007",
                "valid_trajectory_count": 0,
            },
            {
                "slot_id": "slot_03",
                "group_id": "group_001_02",
                "parent_group_id": "group_001",
                "source_segment_id": "segment_0007",
                "valid_trajectory_count": 0,
            },
        ],
    )

    assert "forbidden_group_segment_bindings" not in feedback
    assert feedback["unavailable_source_segment_ids"] == []


def test_provider_failure_never_adds_a_hard_group_segment_binding() -> None:
    feedback = merge_planners_feedback(
        None,
        attempt=1,
        error="provider unavailable",
        diagnostics={
            "failed_parent_group_ids": ["group_001"],
            "reason_code": "model_request_failed",
        },
        failed_slots=[
            {
                "slot_id": "slot_02",
                "parent_group_id": "group_001",
                "source_segment_id": "segment_0007",
                "valid_trajectory_count": 0,
            }
        ],
    )

    assert "forbidden_group_segment_bindings" not in feedback


def test_legacy_semantic_binding_is_dropped_but_static_segment_is_retained() -> None:
    feedback = merge_planners_feedback(
        {
            "forbidden_group_segment_bindings": [
                {
                    "parent_group_id": "group_001",
                    "slot_ids": ["slot_01"],
                    "source_segment_id": "segment_0001",
                }
            ],
            "unavailable_source_segment_ids": ["segment_0099"],
        },
        attempt=2,
        error="local repair",
        diagnostics={},
        failed_slots=[],
    )

    assert "forbidden_group_segment_bindings" not in feedback
    assert feedback["unavailable_source_segment_ids"] == ["segment_0099"]


def test_failure_history_preserves_original_contract_and_distinct_windows() -> None:
    slot = {
        "slot_id": "slot_01",
        "group_id": "group_001",
        "source_segment_id": "segment_0001",
        "content_description": "The goalkeeper catches the ball",
        "required_visible_subjects": ["goalkeeper"],
    }
    rejections = [
        {
            "slot_id": "slot_01",
            "group_id": "group_001",
            "reason_code": "required_subject_not_visually_confirmed",
            "timestamp": timestamp,
            "visible_description": visible,
            "visible_subjects": ["spectators"],
            "visual_evidence": visible,
        }
        for timestamp, visible in [
            ("00:00:01,000-00:00:03,000", "Spectators clap without the goalkeeper"),
            ("00:00:05,000-00:00:07,000", "Spectators wave flags without the goalkeeper"),
        ]
    ]
    first = merge_planners_feedback(
        None,
        attempt=1,
        error="subject not visible",
        diagnostics={"candidate_rejections": [*rejections, rejections[0]]},
        failed_slots=[slot],
    )
    second = merge_planners_feedback(
        first,
        attempt=2,
        error="revised subject not visible",
        diagnostics={"candidate_rejections": [rejections[0]]},
        failed_slots=[
            {
                **slot,
                "content_description": "The striker takes a shot",
                "required_visible_subjects": ["striker"],
            }
        ],
    )

    history = second["candidate_failure_evidence"]
    assert len(history) == 3
    original = [
        item for item in history
        if item["planned_content_description"] == slot["content_description"]
    ]
    assert {item["timestamp"] for item in original} == {
        rejection["timestamp"] for rejection in rejections
    }
    assert sorted(item["occurrences"] for item in original) == [1, 2]
    assert all(item["required_visible_subjects"] == ["goalkeeper"] for item in original)
    assert history[-1]["required_visible_subjects"] == ["striker"]
    assert history[-1]["visible_subjects"] == ["spectators"]


def test_empty_return_preserves_failed_slot_contract_without_fabricated_visuals() -> None:
    feedback = merge_planners_feedback(
        None,
        attempt=1,
        error="No trajectory returned",
        diagnostics={
            "reason_code": "insufficient_visually_grounded_candidates",
            "diagnosis": "The returned batch contained no complete trajectory",
            "semantic_zero_candidate_group_ids": ["group_001"],
            "candidate_rejections": [],
            "group_failures": {"group_001": []},
        },
        failed_slots=[
            {
                "slot_id": "slot_01",
                "group_id": "group_001",
                "source_segment_id": "segment_0001",
                "content_description": "The goalkeeper catches the ball",
                "required_visible_subjects": ["goalkeeper"],
            }
        ],
    )

    evidence, = feedback["candidate_failure_evidence"]
    assert evidence["slot_ids"] == ["slot_01"]
    assert evidence["planned_content_description"] == "The goalkeeper catches the ball"
    assert evidence["required_visible_subjects"] == ["goalkeeper"]
    assert "timestamp" not in evidence
    assert "visible_description" not in evidence
    assert "forbidden_group_segment_bindings" not in feedback


def test_intrinsically_unavailable_segments_accumulate_without_full_diagnostics() -> None:
    first = merge_planners_feedback(
        None,
        attempt=1,
        error="all static",
        diagnostics={"unavailable_source_segment_ids": ["segment_0099"]},
        failed_slots=[],
    )
    second = merge_planners_feedback(
        first,
        attempt=2,
        error="another all-static Segment",
        diagnostics={"unavailable_source_segment_ids": ["segment_0100"]},
        failed_slots=[],
    )

    assert second["unavailable_source_segment_ids"] == [
        "segment_0099",
        "segment_0100",
    ]
    assert "diagnostics" not in second["failure_history"][-1]
    assert "failed_slots" not in second["failure_history"][-1]


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
