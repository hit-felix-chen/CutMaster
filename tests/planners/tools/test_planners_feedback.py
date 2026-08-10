from cutmaster.planners.tools.planners_feedback import merge_planners_feedback


def test_planners_feedback_accumulates_failed_assignments_and_forbidden_segments() -> None:
    first = merge_planners_feedback(
        None,
        attempt=1,
        error="first failure",
        diagnostics={"shortages": {"slot_01": 3}},
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
                "source_segment_ids": ["segment_0007"],
                "missing_candidates": 1,
            }
        ],
        candidates_per_slot=3,
    )

    assert second["forbidden_segment_ids"] == ["segment_0001"]
    assert {
        tuple(item["source_segment_ids"]) for item in second["failed_slots"]
    } == {("segment_0001",), ("segment_0007",)}
    assert [item["attempt"] for item in second["failure_history"]] == [1, 2]
