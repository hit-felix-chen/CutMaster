from __future__ import annotations

from types import SimpleNamespace

import pytest

from cutmaster.configuration.schema import LLMConfig
from cutmaster.workflow.planners.arrangement_architect import (
    RepairDomainAssessment,
    _plan_edit_slots_from_context,
    _repair_blocker_slot_ids_by_slot,
    _targeted_slot_constraints,
    _validate_slots,
    assess_repair_domain,
    redesign_edit_slots,
)


def _slots() -> list[dict[str, object]]:
    return [
        {
            "slot_id": f"slot_{index:02d}",
            "narrative_role": "development",
            "content_description": f"event {index}",
            "target_emotion": "focused",
            "target_emotional_intensity": 0.5,
            "target_kinetic_energy": 0.5,
            "desired_duration_sec": 4.0,
            "planned_duration_sec": 4.0,
            "continuity_from_previous": "continues",
            "source_segment_ids": [f"segment_{index:04d}"],
            "required_visible_subjects": [],
        }
        for index in range(1, 6)
    ]


def _video_description() -> dict[str, object]:
    return {
        "segments": [
            {
                "segment_id": f"segment_{index:04d}",
                "time_range": {
                    "start_sec": float((index - 1) * 10),
                    "end_sec": float(index * 10),
                },
            }
            for index in range(1, 6)
        ]
    }


def test_repair_domain_expands_through_a_contiguous_blocker_chain() -> None:
    assessment = assess_repair_domain(
        _slots(),
        {"slot_02"},
        blocker_slot_ids_by_slot={
            "slot_02": {"slot_03"},
            "slot_03": {"slot_04"},
        },
    )

    assert assessment == RepairDomainAssessment(
        original_slot_ids=frozenset({"slot_02"}),
        repair_slot_ids=frozenset({"slot_02", "slot_03", "slot_04"}),
        blocker_slot_ids=frozenset({"slot_03", "slot_04"}),
        unrepairable_slot_ids=frozenset(),
        reason=None,
    )
    assert assessment.unrepairable is False


def test_repair_domain_expands_contiguously_on_both_sides() -> None:
    assessment = assess_repair_domain(
        _slots(),
        {"slot_03"},
        blocker_slot_ids_by_slot={
            "slot_03": {"slot_02", "slot_04"},
            "slot_02": {"slot_01"},
            "slot_04": {"slot_05"},
        },
    )

    assert assessment.repair_slot_ids == frozenset(
        {"slot_01", "slot_02", "slot_03", "slot_04", "slot_05"}
    )
    assert assessment.blocker_slot_ids == frozenset(
        {"slot_01", "slot_02", "slot_04", "slot_05"}
    )
    assert assessment.unrepairable is False


def test_repair_domain_rejects_a_non_adjacent_blocker_jump() -> None:
    assessment = assess_repair_domain(
        _slots(),
        {"slot_02"},
        blocker_slot_ids_by_slot={"slot_02": {"slot_04"}},
    )

    assert assessment.repair_slot_ids == frozenset({"slot_02"})
    assert assessment.unrepairable_slot_ids == frozenset({"slot_02"})
    assert assessment.unrepairable is True
    assert assessment.reason is not None
    assert "not adjacent" in assessment.reason


def test_repair_domain_marks_a_non_anchor_fixed_candidate_unrepairable() -> None:
    slots = _slots()
    slots[2]["fixed_candidate"] = {"candidate_id": "fixed_visual"}

    assessment = assess_repair_domain(
        slots,
        {"slot_02"},
        blocker_slot_ids_by_slot={"slot_02": {"slot_03"}},
    )

    assert assessment.repair_slot_ids == frozenset({"slot_02"})
    assert assessment.unrepairable_slot_ids == frozenset({"slot_02"})
    assert assessment.unrepairable is True
    assert assessment.reason is not None
    assert "fixed candidate" in assessment.reason


def test_repair_domain_can_expand_through_a_dialogue_anchor() -> None:
    slots = _slots()
    slots[2]["dialogue_anchor"] = {"source_segment_id": "segment_0003"}
    slots[2]["fixed_candidate"] = {"candidate_id": "dialogue_anchor"}

    assessment = assess_repair_domain(
        slots,
        {"slot_02"},
        blocker_slot_ids_by_slot={"slot_02": {"slot_03"}},
    )

    assert assessment.repair_slot_ids == frozenset({"slot_02", "slot_03"})
    assert assessment.unrepairable is False


def test_repair_domain_propagates_intrinsic_unrepairability() -> None:
    assessment = assess_repair_domain(
        _slots(),
        {"slot_02", "slot_04"},
        intrinsically_unrepairable_slot_ids={"slot_04"},
    )

    assert assessment.repair_slot_ids == frozenset({"slot_02", "slot_04"})
    assert assessment.unrepairable_slot_ids == frozenset({"slot_04"})
    assert assessment.unrepairable is True
    assert assessment.reason is not None
    assert "slot_04" in assessment.reason


def test_repair_blockers_merge_direct_and_batched_failure_fields() -> None:
    blockers = _repair_blocker_slot_ids_by_slot(
        [
            {
                "slot_id": "slot_02",
                "repair_blocker_slot_ids": ["slot_03"],
            },
            {
                "slot_id": "slot_04",
                "blocker_slot_ids_by_slot": {
                    "slot_03": ["slot_04"],
                },
            },
        ]
    )

    assert blockers == {
        "slot_02": {"slot_03"},
        "slot_03": {"slot_04"},
    }


def test_unavailable_segments_make_an_empty_targeted_domain_unrepairable() -> None:
    slots = _slots()
    constraints = _targeted_slot_constraints(
        slots,
        {"slot_02"},
        _video_description(),
        unavailable_source_segment_ids={"segment_0002"},
    )

    assessment = assess_repair_domain(
        slots,
        {"slot_02"},
        video_description=_video_description(),
        unavailable_source_segment_ids={"segment_0002"},
    )

    assert constraints["slot_02"]["allowed_segment_ids"] == []
    assert assessment.unrepairable_slot_ids == frozenset({"slot_02"})
    assert assessment.reason is not None
    assert "no available source Segment" in assessment.reason


def test_adjacent_blocker_expansion_can_open_an_available_targeted_domain() -> None:
    slots = _slots()
    slots[3]["source_segment_ids"] = ["segment_0005"]
    slots[4]["source_segment_ids"] = ["segment_0006"]
    video_description = {
        "segments": [
            {
                "segment_id": f"segment_{index:04d}",
                "time_range": {
                    "start_sec": float((index - 1) * 10),
                    "end_sec": float(index * 10),
                },
            }
            for index in range(1, 7)
        ]
    }
    assessment = assess_repair_domain(
        slots,
        {"slot_02"},
        blocker_slot_ids_by_slot={"slot_02": {"slot_03"}},
        video_description=video_description,
        unavailable_source_segment_ids={"segment_0002"},
    )

    assert assessment.repair_slot_ids == frozenset({"slot_02", "slot_03"})
    assert assessment.unrepairable is False


def test_single_valid_new_segment_is_repairable_without_absorbing_fixed_neighbors() -> None:
    slots = _slots()[:3]
    slots[0]["fixed_candidate"] = {"candidate_id": "fixed_left"}
    slots[1]["source_segment_ids"] = ["segment_0002"]
    slots[2]["source_segment_ids"] = ["segment_0004"]
    slots[2]["fixed_candidate"] = {"candidate_id": "fixed_right"}
    video_description = {
        "segments": [
            {
                "segment_id": f"segment_{index:04d}",
                "time_range": {
                    "start_sec": float((index - 1) * 10),
                    "end_sec": float(index * 10),
                },
            }
            for index in range(1, 5)
        ]
    }

    assessment = assess_repair_domain(
        slots,
        {"slot_02"},
        video_description=video_description,
        unavailable_source_segment_ids={"segment_0002"},
    )
    constraints = _targeted_slot_constraints(
        slots,
        set(assessment.repair_slot_ids),
        video_description,
        failed_slot_ids={"slot_02"},
        unavailable_source_segment_ids={"segment_0002"},
    )

    assert assessment.unrepairable is False
    assert assessment.repair_slot_ids == frozenset({"slot_02"})
    assert constraints["slot_02"]["allowed_segment_ids"] == ["segment_0003"]


def test_repair_domain_expands_until_an_ordered_assignment_exists() -> None:
    slots = _slots()
    slots[4]["source_segment_ids"] = ["segment_0008"]
    video_description = {
        "segments": [
            {
                "segment_id": f"segment_{index:04d}",
                "time_range": {
                    "start_sec": float((index - 1) * 10),
                    "end_sec": float(index * 10),
                },
            }
            for index in range(1, 9)
        ]
    }

    assessment = assess_repair_domain(
        slots,
        {"slot_02"},
        blocker_slot_ids_by_slot={"slot_02": {"slot_03"}},
        video_description=video_description,
        unavailable_source_segment_ids={"segment_0002"},
    )

    assert assessment.unrepairable is False
    assert assessment.repair_slot_ids.issuperset(
        {"slot_02", "slot_03", "slot_04"}
    )


def test_repair_domain_rejects_segments_that_are_all_too_short() -> None:
    video_description = {
        "segments": [
            {
                "segment_id": f"segment_{index:04d}",
                "time_range": {
                    "start_sec": float(index - 1),
                    "end_sec": float(index),
                },
            }
            for index in range(1, 6)
        ]
    }

    assessment = assess_repair_domain(
        _slots(),
        {"slot_02"},
        video_description=video_description,
    )

    assert assessment.unrepairable_slot_ids == frozenset({"slot_02"})
    assert assessment.reason is not None
    assert "longer than planned_duration_sec" in assessment.reason


def test_repair_domain_excludes_every_segment_in_a_failed_multi_segment_binding() -> None:
    slots = _slots()[:3]
    slots[1]["source_segment_ids"] = ["segment_0002", "segment_0003"]
    slots[2]["source_segment_ids"] = ["segment_0004"]
    video_description = {
        "segments": [
            {
                "segment_id": f"segment_{index:04d}",
                "time_range": {
                    "start_sec": float((index - 1) * 10),
                    "end_sec": float(index * 10),
                },
            }
            for index in range(1, 5)
        ]
    }

    assessment = assess_repair_domain(
        slots,
        {"slot_02"},
        video_description=video_description,
    )

    assert assessment.unrepairable is True
    assert assessment.unrepairable_slot_ids == frozenset({"slot_02"})
    assert assessment.reason is not None
    assert "strictly increasing" in assessment.reason


def test_full_arrangement_excludes_unavailable_segments_from_model_domain() -> None:
    video_description = _video_description()

    class FakeContext:
        def __init__(self) -> None:
            self.package = None

        @staticmethod
        def get_artifact(name: str):
            return {
                "video_description": video_description,
                "video_summary": {"synopsis": "A match."},
                "planners_feedback": {
                    "unavailable_source_segment_ids": ["segment_0002"],
                },
                "unavailable_source_segment_ids": ["segment_0003"],
            }.get(name)

        def call_prompt(self, *, package, config, validate_business):
            self.package = package
            unavailable_response = {
                "slots": [
                    {
                        "narrative_role": "setup",
                        "content_description": "unsupported",
                        "target_emotion": "focused",
                        "target_emotional_intensity": 0.5,
                        "target_kinetic_energy": 0.5,
                        "desired_duration_sec": 4.0,
                        "continuity_from_previous": "opening",
                        "source_segment_ids": ["segment_0002"],
                        "required_visible_subjects": [],
                    }
                ]
            }
            try:
                validate_business(unavailable_response)
            except ValueError as exc:
                assert "visually disproven" in str(exc)
            else:
                raise AssertionError("Unavailable Segment passed full validation")
            return []

    context = FakeContext()
    _plan_edit_slots_from_context(
        SimpleNamespace(target_output_length_sec=4.0),
        {},
        LLMConfig(model="test", base_url="", api_key="test"),
        context,
        target_clip_duration_sec=4.0,
    )

    source_segment_enum = context.package.response_contract.schema["properties"][
        "slots"
    ]["items"]["properties"]["source_segment_ids"]["items"]["enum"]
    assert "segment_0002" not in source_segment_enum
    assert "segment_0003" not in source_segment_enum


def test_full_arrangement_only_hard_forbids_zero_candidate_assignments() -> None:
    video_description = _video_description()

    class FakeContext:
        def __init__(self) -> None:
            self.package = None

        @staticmethod
        def get_artifact(name: str):
            return {
                "video_description": video_description,
                "video_summary": {"synopsis": "A match."},
                "planners_feedback": {
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
                    ]
                },
            }.get(name)

        def call_prompt(self, *, package, config, validate_business):
            self.package = package
            shortage_assignment = {
                "slots": [
                    {
                        "narrative_role": "setup",
                        "content_description": "first event",
                        "target_emotion": "focused",
                        "target_emotional_intensity": 0.5,
                        "target_kinetic_energy": 0.5,
                        "desired_duration_sec": 4.0,
                        "continuity_from_previous": "opening",
                        "source_segment_ids": ["segment_0001"],
                        "required_visible_subjects": [],
                    },
                    {
                        "narrative_role": "development",
                        "content_description": "later event",
                        "target_emotion": "focused",
                        "target_emotional_intensity": 0.5,
                        "target_kinetic_energy": 0.5,
                        "desired_duration_sec": 4.0,
                        "continuity_from_previous": "continues",
                        "source_segment_ids": ["segment_0003"],
                        "required_visible_subjects": [],
                    },
                ]
            }
            assert validate_business(shortage_assignment)[0][
                "source_segment_ids"
            ] == ["segment_0001"]

            forbidden_assignment = {
                "slots": [
                    shortage_assignment["slots"][0],
                    {
                        **shortage_assignment["slots"][1],
                        "source_segment_ids": ["segment_0002"],
                    },
                ]
            }
            with pytest.raises(ValueError) as exc_info:
                validate_business(forbidden_assignment)
            message = str(exc_info.value)
            assert "slot_02" in message
            assert "attempted assignment=['segment_0002']" in message
            assert "forbidden assignments=[['segment_0002']]" in message
            return []

    context = FakeContext()
    _plan_edit_slots_from_context(
        SimpleNamespace(target_output_length_sec=8.0),
        {},
        LLMConfig(model="test", base_url="", api_key="test"),
        context,
        target_clip_duration_sec=4.0,
        candidates_per_slot=3,
    )

    assert (
        '<hard_forbidden_assignments>\n{"slot_02": [["segment_0002"]]}\n'
        '</hard_forbidden_assignments>'
    ) in context.package.user_prompt


def test_failed_assignment_is_scoped_to_its_original_slot() -> None:
    raw = {
        "slots": [
            {
                "narrative_role": "setup",
                "content_description": "first event",
                "target_emotion": "focused",
                "target_emotional_intensity": 0.5,
                "target_kinetic_energy": 0.5,
                "desired_duration_sec": 4.0,
                "continuity_from_previous": "opening",
                "source_segment_ids": ["segment_0001"],
                "required_visible_subjects": [],
            },
            {
                "narrative_role": "development",
                "content_description": "second event",
                "target_emotion": "focused",
                "target_emotional_intensity": 0.5,
                "target_kinetic_energy": 0.5,
                "desired_duration_sec": 4.0,
                "continuity_from_previous": "continues",
                "source_segment_ids": ["segment_0002"],
                "required_visible_subjects": [],
            },
        ]
    }

    slots = _validate_slots(
        raw,
        8.0,
        4.0,
        _video_description(),
        set(),
        hard_forbidden_assignments={
            "slot_01": {("segment_0002",)},
        },
    )

    assert slots[1]["source_segment_ids"] == ["segment_0002"]


def test_full_arrangement_rejects_reversed_source_segment_ids() -> None:
    raw = {
        "slots": [
            {
                "narrative_role": "setup",
                "content_description": "first event",
                "target_emotion": "focused",
                "target_emotional_intensity": 0.5,
                "target_kinetic_energy": 0.5,
                "desired_duration_sec": 4.0,
                "continuity_from_previous": "opening",
                "source_segment_ids": ["segment_0002", "segment_0001"],
                "required_visible_subjects": [],
            }
        ]
    }

    with pytest.raises(ValueError, match="must follow source order"):
        _validate_slots(
            raw,
            4.0,
            4.0,
            _video_description(),
            set(),
            hard_forbidden_assignments={},
        )


def test_collateral_slot_constraints_exclude_its_own_failed_segment_history() -> None:
    slots = _slots()
    slots[3]["source_segment_ids"] = ["segment_0006"]
    slots[4]["source_segment_ids"] = ["segment_0007"]
    video_description = {
        "segments": [
            {
                "segment_id": f"segment_{index:04d}",
                "time_range": {
                    "start_sec": float((index - 1) * 10),
                    "end_sec": float(index * 10),
                },
            }
            for index in range(1, 8)
        ]
    }

    constraints = _targeted_slot_constraints(
        slots,
        {"slot_02", "slot_03"},
        video_description,
        failed_slot_ids={"slot_02"},
        failed_source_segment_ids_by_slot={
            "slot_02": {"segment_0002"},
            "slot_03": {"segment_0004"},
        },
    )

    assert constraints["slot_02"]["allowed_segment_ids"] == [
        "segment_0003",
        "segment_0004",
        "segment_0005",
    ]
    assert constraints["slot_03"]["allowed_segment_ids"] == [
        "segment_0002",
        "segment_0003",
        "segment_0005",
    ]
    assert "segment_0003" in constraints["slot_03"]["allowed_segment_ids"]
    assert "segment_0004" not in constraints["slot_03"]["allowed_segment_ids"]


def test_targeted_repair_merges_batched_history_for_expanded_blocker_slot() -> None:
    slots = _slots()
    slots[3]["source_segment_ids"] = ["segment_0006"]
    slots[4]["source_segment_ids"] = ["segment_0007"]
    video_description = {
        "segments": [
            {
                "segment_id": f"segment_{index:04d}",
                "time_range": {
                    "start_sec": float((index - 1) * 10),
                    "end_sec": float(index * 10),
                },
            }
            for index in range(1, 8)
        ]
    }
    failures = [
        {
            "slot_id": "slot_02",
            "reason_code": "required_subject_not_visually_confirmed",
            "repair_blocker_slot_ids": ["slot_03"],
            "failed_source_segment_ids": ["segment_0002"],
            "failed_source_segment_ids_by_slot": {
                "slot_03": ["segment_0004"],
            },
        }
    ]

    class FakeContext:
        def __init__(self) -> None:
            self.edit_plan = None

        @staticmethod
        def get_artifact(name: str):
            return {
                "video_description": video_description,
                "planners_feedback": None,
                "unavailable_source_segment_ids": [],
            }.get(name)

        def call_prompt(self, *, package, config, validate_business):
            schemas = package.response_contract.schema["properties"]["slots"][
                "items"
            ]["oneOf"]
            allowed_by_slot = {
                schema["properties"]["slot_id"]["const"]: schema["properties"][
                    "source_segment_ids"
                ]["items"]["enum"]
                for schema in schemas
            }
            assert "segment_0004" not in allowed_by_slot["slot_03"]
            replacements = []
            for slot_id, source_segment_ids in (
                ("slot_02", ["segment_0003"]),
                ("slot_03", ["segment_0005"]),
            ):
                original = next(slot for slot in slots if slot["slot_id"] == slot_id)
                replacements.append(
                    {
                        **original,
                        "source_segment_ids": source_segment_ids,
                    }
                )
            return validate_business({"slots": replacements})

        def set_artifact(self, name: str, value):
            if name == "edit_plan":
                self.edit_plan = value

    redesigned, repaired_slot_ids = redesign_edit_slots(
        slots,
        failures,
        LLMConfig(model="test", base_url="", api_key="test"),
        FakeContext(),
    )

    assert repaired_slot_ids == {"slot_02", "slot_03"}
    assert redesigned[1]["source_segment_ids"] == ["segment_0003"]
    assert redesigned[2]["source_segment_ids"] == ["segment_0005"]
