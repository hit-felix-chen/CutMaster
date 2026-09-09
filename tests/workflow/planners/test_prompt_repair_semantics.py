import pytest

from cutmaster.workflow.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)


@pytest.mark.parametrize(
    ("reason_code", "details"),
    [
        (
            PromptFailureCode.REQUIRED_SUBJECT_NOT_VISUALLY_CONFIRMED,
            {
                "candidate_id": "candidate_01",
                "timestamp": "00:48:51,000-00:48:54,000",
                "required_visible_subjects": ["named defender"],
                "visual_evidence": "The goal is visible, but the player's identity is not.",
            },
        ),
        (
            PromptFailureCode.NO_CANDIDATE_PASSED_VISUAL_DIAGNOSTICS,
            {"candidate_rejections": []},
        ),
        (
            PromptFailureCode.INSUFFICIENT_VISUALLY_GROUNDED_CANDIDATES,
            {"shortages": {"group_002": 0}},
        ),
    ],
)
def test_zero_candidate_advice_allows_original_legal_segment(reason_code, details) -> None:
    failure = build_prompt_failure(reason_code, **details)

    assert "may keep the original Segment" in failure["repair_requirement"]
    assert "Choose a different Segment" not in failure["repair_requirement"]
    assert "split the narrative requirement" not in failure["repair_requirement"]
    for key, value in details.items():
        assert failure[key] == value


def test_static_candidate_diagnostic_does_not_declare_entire_segment_unavailable() -> None:
    failure = build_prompt_failure(
        PromptFailureCode.VISUALLY_STATIC,
        candidate_id="candidate_01",
        timestamp="00:48:51,000-00:48:54,000",
        kinetic_energy=0.01,
        static_threshold=0.05,
    )

    assert "Do not reuse this timestamp" in failure["repair_requirement"]
    assert "Choose a different Segment" not in failure["repair_requirement"]
    assert "unavailable_source_segment_ids" not in failure
