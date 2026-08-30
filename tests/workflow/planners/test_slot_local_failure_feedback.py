from __future__ import annotations

from copy import deepcopy

import pytest

from cutmaster.workflow.planners.arrangement_architect import (
    _targeted_slot_constraints,
    _validate_targeted_slots,
)
from cutmaster.workflow.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)


def _video_description() -> dict[str, object]:
    return {
        "segments": [
            {
                "segment_id": segment_id,
                "time_range": {"start_sec": start, "end_sec": start + 10.0},
            }
            for segment_id, start in (
                ("segment_0001", 0.0),
                ("segment_0002", 10.0),
                ("segment_0003", 20.0),
            )
        ]
    }


def _slot() -> dict[str, object]:
    return {
        "slot_id": "slot_01",
        "narrative_role": "proof",
        "content_description": "Vozinha makes a decisive save at the goal line.",
        "target_emotion": "tense",
        "target_emotional_intensity": 0.8,
        "target_kinetic_energy": 0.7,
        "desired_duration_sec": 4.0,
        "planned_duration_sec": 4.0,
        "continuity_from_previous": "Continue the defensive sequence.",
        "source_segment_ids": ["segment_0001"],
        "required_visible_subjects": ["Vozinha"],
    }


def _replacement(slot: dict[str, object], **changes: object) -> dict[str, object]:
    replacement = deepcopy(slot)
    replacement.update(changes)
    return {"slots": [replacement]}


def _constraints(slot: dict[str, object]) -> dict[str, dict[str, object]]:
    return _targeted_slot_constraints(
        [slot],
        {"slot_01"},
        _video_description(),
        failed_slot_ids={"slot_01"},
    )


def _failure(*reason_codes: PromptFailureCode) -> list[dict[str, object]]:
    return [
        {
            "slot_id": "slot_01",
            "reason_code": (
                PromptFailureCode.NO_CANDIDATE_PASSED_VISUAL_DIAGNOSTICS.value
            ),
            "candidate_rejections": [
                {"reason_code": reason_code.value} for reason_code in reason_codes
            ],
        }
    ]


@pytest.mark.parametrize("replacement_subjects", [[], ["Cape Verde players"]])
def test_subject_failure_cannot_be_repaired_by_weakening_subject_contract(
    replacement_subjects: list[str],
) -> None:
    slot = _slot()

    with pytest.raises(ValueError, match="required_visible_subjects"):
        _validate_targeted_slots(
            _replacement(
                slot,
                source_segment_ids=["segment_0002"],
                required_visible_subjects=replacement_subjects,
            ),
            [slot],
            _constraints(slot),
            _video_description(),
            failures=_failure(
                PromptFailureCode.REQUIRED_SUBJECT_NOT_VISUALLY_CONFIRMED
            ),
        )


def test_static_failure_changes_source_without_rewriting_slot_semantics() -> None:
    slot = _slot()

    with pytest.raises(ValueError, match="content_description"):
        _validate_targeted_slots(
            _replacement(
                slot,
                source_segment_ids=["segment_0002"],
                content_description="A generic crowd celebration.",
            ),
            [slot],
            _constraints(slot),
            _video_description(),
            failures=_failure(PromptFailureCode.VISUALLY_STATIC),
        )


def test_subject_failure_must_leave_all_previous_source_segments() -> None:
    slot = _slot()
    constraints = _constraints(slot)

    assert constraints["slot_01"]["allowed_segment_ids"] == [
        "segment_0002",
        "segment_0003",
    ]
    with pytest.raises(ValueError, match="outside its chronological interval"):
        _validate_targeted_slots(
            _replacement(
                slot,
                source_segment_ids=["segment_0001", "segment_0002"],
            ),
            [slot],
            constraints,
            _video_description(),
            failures=_failure(
                PromptFailureCode.REQUIRED_SUBJECT_NOT_VISUALLY_CONFIRMED
            ),
        )


def test_targeted_domain_excludes_all_earlier_failed_segments_for_the_slot() -> None:
    slot = _slot()
    slot["source_segment_ids"] = ["segment_0002"]
    constraints = _targeted_slot_constraints(
        [slot],
        {"slot_01"},
        _video_description(),
        failed_slot_ids={"slot_01"},
        failed_source_segment_ids_by_slot={
            "slot_01": {"segment_0001", "segment_0002"},
        },
    )

    assert constraints["slot_01"]["allowed_segment_ids"] == ["segment_0003"]


def test_relevance_failure_may_change_event_but_preserves_required_subjects() -> None:
    slot = _slot()
    repaired = _validate_targeted_slots(
        _replacement(
            slot,
            source_segment_ids=["segment_0002"],
            content_description="Vozinha dives across goal and palms the ball away.",
        ),
        [slot],
        _constraints(slot),
        _video_description(),
        failures=_failure(PromptFailureCode.VISUAL_SLOT_NOT_RELEVANT),
    )

    assert repaired[0]["required_visible_subjects"] == ["Vozinha"]
    assert repaired[0]["source_segment_ids"] == ["segment_0002"]


def test_subject_failure_catalog_forbids_deleting_or_renaming_subjects() -> None:
    failure = build_prompt_failure(
        PromptFailureCode.REQUIRED_SUBJECT_NOT_VISUALLY_CONFIRMED,
        candidate_id="candidate_01",
        timestamp="00:00:01,000-00:00:05,000",
        required_visible_subjects=["Vozinha"],
        visual_evidence="The goalkeeper is not visible.",
    )

    assert "Keep required_visible_subjects unchanged" in failure["repair_requirement"]
    assert "delete or rename" in failure["repair_requirement"]
