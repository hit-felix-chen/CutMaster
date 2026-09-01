from __future__ import annotations

from types import SimpleNamespace

import pytest

from cutmaster.configuration.schema import LLMConfig
from cutmaster.workflow.planners.aster_team import ASTERTeam
from cutmaster.workflow.planners.arrangement_architect import (
    ArrangementArchitectAgent,
    _arrangement_groups,
    _expand_target_group_slot_ids,
    _plan_edit_slots_from_context,
    _repair_window_slot_ids,
    _validate_and_align_slots,
    _validate_group_capacity,
    _validate_slots,
    align_slots_to_music,
)


def test_arrangement_agent_caps_each_plan_to_three_model_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received_retries: list[int] = []

    def arrange_stub(_request, _music, model_config, *_args, **_kwargs):
        received_retries.append(model_config.max_retries)
        return []

    def repair_stub(_slots, _failures, model_config, *_args, **_kwargs):
        received_retries.append(model_config.max_retries)
        return [], set()

    monkeypatch.setattr(
        "cutmaster.workflow.planners.arrangement_architect.plan_edit_slots",
        arrange_stub,
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.arrangement_architect.redesign_edit_slots",
        repair_stub,
    )
    config = SimpleNamespace(
        llm=LLMConfig(
            model="test",
            base_url="",
            api_key="test",
            max_retries=9,
        ),
        planners=SimpleNamespace(
            arrangement_architect=SimpleNamespace(
                target_clip_duration_sec=4.0,
                max_model_requests=3,
            )
        ),
        renderer=SimpleNamespace(fps=30),
    )
    agent = ArrangementArchitectAgent(config, SimpleNamespace())

    agent.arrange(SimpleNamespace(), {})
    agent.repair([], [])

    assert received_retries == [2, 2]


def _video_description(*, duration_sec: float = 10.0) -> dict:
    return {
        "segments": [
            {
                "segment_id": f"segment_{index:04d}",
                "time_range": {
                    "start_sec": float(index * 20),
                    "end_sec": float(index * 20) + duration_sec,
                },
            }
            for index in range(1, 13)
        ]
    }


def _raw_slots(segment_numbers: list[int], durations: list[float]) -> dict:
    return {
        "slots": [
            {
                "narrative_role": "development",
                "content_description": f"event {index}",
                "target_emotion": "focused",
                "target_emotional_intensity": 0.5,
                "target_kinetic_energy": 0.5,
                "desired_duration_sec": duration,
                "continuity_from_previous": "continues",
                "source_segment_id": f"segment_{segment_number:04d}",
                "required_visible_subjects": [],
            }
            for index, (segment_number, duration) in enumerate(
                zip(segment_numbers, durations, strict=True),
                1,
            )
        ]
    }


def test_repeated_adjacent_segments_form_deterministic_groups() -> None:
    slots = _validate_slots(
        _raw_slots([10, 10, 11], [2.0, 2.0, 2.0]),
        6.0,
        2.0,
        _video_description(),
    )

    assert [slot["source_segment_id"] for slot in slots] == [
        "segment_0010",
        "segment_0010",
        "segment_0011",
    ]
    assert [slot["group_id"] for slot in slots] == [
        "group_001",
        "group_001",
        "group_002",
    ]
    assert _arrangement_groups(slots) == [
        {
            "group_id": "group_001",
            "slot_ids": ["slot_01", "slot_02"],
            "source_segment_id": "segment_0010",
        },
        {
            "group_id": "group_002",
            "slot_ids": ["slot_03"],
            "source_segment_id": "segment_0011",
        },
    ]


def test_segment_order_may_repeat_but_may_not_reverse() -> None:
    with pytest.raises(ValueError, match="Source Group Segments.*strictly increasing"):
        _validate_slots(
            _raw_slots([10, 11, 10], [2.0, 2.0, 2.0]),
            6.0,
            2.0,
            _video_description(),
        )


def test_group_capacity_accepts_equality_and_rejects_one_millisecond_overflow() -> None:
    slots = _validate_slots(
        _raw_slots([10, 10], [2.0, 2.0]),
        4.0,
        2.0,
        _video_description(duration_sec=4.0),
    )
    aligned = align_slots_to_music(
        slots,
        {"accents_sec": [2.0], "beats_sec": [2.0]},
        4.0,
        1000,
        2.0,
    )

    _validate_group_capacity(aligned, _video_description(duration_sec=4.0))
    assert sum(slot["planned_duration_ms"] for slot in aligned) == 4000

    overflow = [dict(slot) for slot in aligned]
    overflow[-1]["planned_duration_ms"] += 1
    overflow[-1]["planned_duration_sec"] = (
        overflow[-1]["planned_duration_ms"] / 1000.0
    )
    with pytest.raises(
        ValueError,
        match=(
            r"group_001.*segment_0010.*available=4000ms required=4001ms"
        ),
    ):
        _validate_group_capacity(
            overflow,
            _video_description(duration_sec=4.0),
        )


def test_capacity_is_checked_after_music_alignment() -> None:
    parsed = _raw_slots([10, 11, 11], [2.0, 3.0, 3.0])

    with pytest.raises(
        ValueError,
        match=r"group_002.*available=6000ms required=6500ms",
    ):
        _validate_and_align_slots(
            parsed,
            8.0,
            3.0,
            _video_description(duration_sec=6.0),
            {
                "accents_sec": [1.5, 5.0],
                "beats_sec": [1.5, 5.0],
            },
            2,
        )


def test_targeted_replan_expands_one_slot_to_its_complete_group() -> None:
    slots = _validate_and_align_slots(
        _raw_slots([10, 10, 11], [2.0, 2.0, 2.0]),
        6.0,
        2.0,
        _video_description(),
        {"accents_sec": [2.0, 4.0], "beats_sec": [2.0, 4.0]},
        1000,
    )

    assert _expand_target_group_slot_ids(slots, {"slot_02"}) == {
        "slot_01",
        "slot_02",
    }


def test_targeted_replan_keeps_the_smallest_feasible_group_window() -> None:
    slots = _validate_and_align_slots(
        _raw_slots([1, 2, 3, 4], [2.0, 2.0, 2.0, 2.0]),
        8.0,
        2.0,
        _video_description(),
        {
            "accents_sec": [2.0, 4.0, 6.0],
            "beats_sec": [2.0, 4.0, 6.0],
        },
        1000,
    )

    selected = _repair_window_slot_ids(
        slots,
        {"slot_02"},
        _video_description(),
    )

    assert selected == {"slot_02"}


def test_alignment_publishes_milliseconds_as_authoritative_duration() -> None:
    slots = _validate_slots(
        _raw_slots([10, 11, 12], [2.0, 2.0, 2.0]),
        6.0,
        2.0,
        _video_description(),
    )

    aligned = align_slots_to_music(
        slots,
        {"accents_sec": [2.0, 4.0], "beats_sec": [2.0, 4.0]},
        6.0,
        30,
        2.0,
    )

    assert [slot["planned_duration_ms"] for slot in aligned] == [2000, 2000, 2000]
    assert all(
        slot["planned_duration_sec"] == slot["planned_duration_ms"] / 1000.0
        for slot in aligned
    )


def test_post_alignment_capacity_failure_stays_inside_prompt_retry_boundary() -> None:
    artifacts = {
        "video_description": _video_description(duration_sec=6.0),
        "video_summary": {"summary": "test"},
    }
    invalid = _raw_slots([10, 11, 11], [2.0, 3.0, 3.0])
    valid = _raw_slots([10, 11, 12], [2.0, 3.0, 3.0])
    failures: list[str] = []

    class FakeContext:
        def get_artifact(self, name, default=None):
            return artifacts.get(name, default)

        def set_artifact(self, name, value):
            artifacts[name] = value

        def call_prompt(self, *, validate_business, **_kwargs):
            try:
                validate_business(invalid)
            except ValueError as exc:
                failures.append(str(exc))
            return validate_business(valid)

    result = _plan_edit_slots_from_context(
        SimpleNamespace(target_output_length_sec=8.0),
        {"accents_sec": [1.5, 5.0], "beats_sec": [1.5, 5.0]},
        LLMConfig(model="test", base_url="", api_key="test"),
        FakeContext(),
        target_clip_duration_sec=3.0,
        output_fps=2,
    )

    assert len(failures) == 1
    assert "available=6000ms required=6500ms" in failures[0]
    assert [slot["group_id"] for slot in result] == [
        "group_001",
        "group_002",
        "group_003",
    ]
    assert artifacts["arrangement_groups"] == _arrangement_groups(result)


def test_repair_groups_maps_story_child_to_complete_parent_before_story() -> None:
    def slot(slot_id: str, group_id: str, *, parent_group_id: str | None = None):
        item = {
            "slot_id": slot_id,
            "group_id": group_id,
            "source_segment_id": "segment_0001",
            "content_description": f"event {slot_id}",
        }
        if parent_group_id is not None:
            item["parent_group_id"] = parent_group_id
        return item

    runtime_slots = [
        slot("slot_01", "group_001_01", parent_group_id="group_001"),
        slot("slot_02", "group_001_anchor_01", parent_group_id="group_001"),
        slot("slot_03", "group_001_02", parent_group_id="group_001"),
        slot("slot_04", "group_002"),
    ]
    arrangement_slots = [
        slot("slot_01", "group_001"),
        slot("slot_02", "group_001"),
        slot("slot_03", "group_001"),
        slot("slot_04", "group_002"),
    ]
    repaired_slots = [
        {**item, "source_segment_id": "segment_0002"}
        if item["group_id"] == "group_001"
        else item
        for item in arrangement_slots
    ]
    refreshed_slots = [
        slot("slot_01", "group_001_01", parent_group_id="group_001"),
        slot("slot_02", "group_001_anchor_01", parent_group_id="group_001"),
        slot("slot_03", "group_001_02", parent_group_id="group_001"),
        slot("slot_04", "group_002"),
    ]
    calls: list[tuple[str, object]] = []

    class StoryEditor:
        def restore_arrangement_slots(self, received):
            calls.append(("restore", received))
            return arrangement_slots

        def anchor(self, received):
            calls.append(("anchor", received))
            return refreshed_slots

    class ArrangementArchitect:
        def repair(self, received, failures):
            calls.append(("repair", (received, failures)))
            return repaired_slots, {"slot_01", "slot_02", "slot_03"}

    class TimelineScout:
        def scout(self, received, cancellation_token=None):
            calls.append(("scout", received))
            return {"group_001_01": [{"trajectory_id": "trajectory_001"}]}

    team = ASTERTeam.__new__(ASTERTeam)
    team.story_editor = StoryEditor()
    team.arrangement_architect = ArrangementArchitect()
    team.timeline_scout = TimelineScout()

    repaired, replanned = team.repair_groups(
        runtime_slots,
        {"failed_group_ids": ["group_001_02"]},
    )
    pool = team.scout(repaired)

    repair_input, failures = calls[1][1]
    assert repair_input == arrangement_slots
    assert [failure["slot_id"] for failure in failures] == [
        "slot_01",
        "slot_02",
        "slot_03",
    ]
    assert calls[2] == ("scout", repaired_slots)
    assert repaired == repaired_slots
    assert replanned == {"slot_01", "slot_02", "slot_03"}
    assert pool["group_001_01"][0]["trajectory_id"] == "trajectory_001"
