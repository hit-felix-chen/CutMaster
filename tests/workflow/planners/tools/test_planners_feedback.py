from cutmaster.workflow.planners.tools.planners_feedback import merge_planners_feedback


def test_planners_feedback_accumulates_failed_assignments_and_forbidden_segments() -> None:
    first = merge_planners_feedback(
        None,
        attempt=1,
        error="first failure",
        diagnostics={
            "shortages": {"slot_01": 3},
            "unavailable_source_segment_ids": ["segment_0099"],
        },
        failed_slots=[
            {
                "slot_id": "slot_01",
                "source_segment_ids": ["segment_0001"],
                "missing_candidates": 3,
            }
        ],
        candidates_per_slot=3,
    )
    second = merge_planners_feedback(
        first,
        attempt=2,
        error="second failure",
        diagnostics={"shortages": {"slot_06": 1}},
        failed_slots=[
            {
                "slot_id": "slot_06",
                # The same Segment assignment remains independently usable for
                # another Slot; failures are keyed by Slot plus assignment.
                "source_segment_ids": ["segment_0001"],
                "missing_candidates": 1,
            }
        ],
        candidates_per_slot=3,
    )

    assert second["unavailable_source_segment_ids"] == ["segment_0099"]
    assert "forbidden_segment_ids" not in second
    assert {
        (item["slot_id"], tuple(item["source_segment_ids"]))
        for item in second["failed_slots"]
    } == {
        ("slot_01", ("segment_0001",)),
    }
    assert [item["attempt"] for item in second["failure_history"]] == [1, 2]
    assert second["failure_history"][1]["failed_slot_ids"] == []
    assert all(
        "diagnostics" not in item and "failed_slots" not in item
        for item in second["failure_history"]
    )
    assert "diagnostics" not in second
    assert "current_failed_slots" not in second


def test_planners_feedback_preserves_pre_repair_evidence_and_deduplicates_by_slot_assignment() -> None:
    first = merge_planners_feedback(
        None,
        attempt=1,
        error="initial retrieval failed",
        diagnostics={},
        failed_slots=[
            {
                "slot_id": "slot_02",
                "source_segment_ids": ["segment_0010"],
                "missing_candidates": 3,
                "reason_code": "no_candidate_passed_visual_diagnostics",
                "candidate_rejections": [
                    {
                        "candidate_id": "candidate_initial",
                        "reason_code": "required_subject_not_visually_confirmed",
                        "visual_evidence": "Only a broadcast bumper is visible.",
                    }
                ],
            }
        ],
        candidates_per_slot=3,
    )

    second = merge_planners_feedback(
        first,
        attempt=2,
        error="repair retrieval failed",
        diagnostics={},
        failed_slots=[
            {
                "slot_id": "slot_02",
                "source_segment_ids": ["segment_0010"],
                "missing_candidates": 3,
                "candidate_rejections": [
                    {
                        "candidate_id": "candidate_initial_retry",
                        "reason_code": "visually_static",
                        "visual_evidence": "The repeated window is static.",
                    }
                ],
            },
            {
                "slot_id": "slot_02",
                "source_segment_ids": ["segment_0011"],
                "missing_candidates": 3,
                "reason_code": "no_candidate_passed_visual_diagnostics",
                "candidate_rejections": [
                    {
                        "candidate_id": "candidate_repair",
                        "reason_code": "visual_slot_not_relevant",
                        "visual_evidence": "The goalkeeper action is unrelated.",
                    }
                ],
            },
        ],
        candidates_per_slot=3,
    )

    by_assignment = {
        (item["slot_id"], tuple(item["source_segment_ids"])): item
        for item in second["failed_slots"]
    }
    assert set(by_assignment) == {
        ("slot_02", ("segment_0010",)),
        ("slot_02", ("segment_0011",)),
    }
    assert {
        rejection["candidate_id"]
        for rejection in by_assignment[
            ("slot_02", ("segment_0010",))
        ]["candidate_rejections"]
    } == {"candidate_initial", "candidate_initial_retry"}
    assert by_assignment[("slot_02", ("segment_0011",))][
        "candidate_rejections"
    ][0]["visual_evidence"] == "The goalkeeper action is unrelated."
    assert "forbidden_segment_ids" not in second


def test_planners_feedback_permanently_accumulates_only_explicitly_unavailable_segments() -> None:
    first = merge_planners_feedback(
        {
            # Legacy checkpoints used this field for Slot-specific semantic
            # failures, so it must not be promoted into the intrinsic global
            # blacklist.
            "forbidden_segment_ids": ["segment_0098"],
        },
        attempt=2,
        error="semantic mismatch",
        diagnostics={
            "unavailable_source_segment_ids": ["segment_0099"],
        },
        failed_slots=[
            {
                "slot_id": "slot_03",
                "source_segment_ids": ["segment_0003"],
                "missing_candidates": 3,
                "candidate_rejections": [
                    {
                        "reason_code": "required_subject_not_visually_confirmed",
                        "visual_evidence": "Identity cannot be verified.",
                    }
                ],
            }
        ],
        candidates_per_slot=3,
    )
    second = merge_planners_feedback(
        first,
        attempt=3,
        error="another mismatch",
        diagnostics={
            "unavailable_source_segment_ids": ["segment_0100"],
        },
        failed_slots=[],
        candidates_per_slot=3,
    )

    assert second["unavailable_source_segment_ids"] == [
        "segment_0099",
        "segment_0100",
    ]
    assert "forbidden_segment_ids" not in second
    assert "segment_0003" not in second["unavailable_source_segment_ids"]


def test_planners_feedback_drops_legacy_and_current_partial_shortages() -> None:
    feedback = merge_planners_feedback(
        {
            "failed_slots": [
                {
                    "slot_id": "slot_01",
                    "source_segment_ids": ["segment_0001"],
                    "missing_candidates": 2,
                },
                {
                    "slot_id": "slot_02",
                    "source_segment_ids": ["segment_0002"],
                    "missing_candidates": 3,
                },
            ],
        },
        attempt=2,
        error="one candidate still remains",
        diagnostics={"shortages": {"slot_03": 1}},
        failed_slots=[
            {
                "slot_id": "slot_03",
                "source_segment_ids": ["segment_0003"],
                "missing_candidates": 1,
            }
        ],
        candidates_per_slot=3,
    )

    assert [item["slot_id"] for item in feedback["failed_slots"]] == [
        "slot_02"
    ]
    assert feedback["failed_slots"][0]["hard_failure"] is True
    assert feedback["failure_history"][-1]["failed_slot_ids"] == []
