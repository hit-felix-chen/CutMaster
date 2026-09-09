from __future__ import annotations

from copy import deepcopy

import pytest

from cutmaster.configuration.schema import LLMConfig
from cutmaster.workflow.planners.arrangement_architect import (
    _repair_window_slot_ids,
    _targeted_slot_constraints,
    _validate_and_align_slots,
    redesign_edit_slots,
)


def _video() -> dict:
    return {
        "source": {"video_name": "match.mp4"},
        "segments": [
            {
                "segment_id": f"segment_{index:04d}",
                "time_range": {"start_sec": (index - 1) * 10, "end_sec": index * 10},
                "has_dialogue": False,
                "speech_mode": "none",
                "content_type": "narrative",
                "timeline_role": "main",
                "segment_summary": f"Source evidence for match event {index}",
                "narrative_function": f"Match event {index}",
                "emotional_tone": "excited",
                "emotional_intensity": 0.7,
                "appearing_characters": [f"Player {index}"],
            }
            for index in range(1, 4)
        ],
    }


def _slots(video: dict) -> list[dict]:
    return _validate_and_align_slots(
        {
            "slots": [
                {
                    "narrative_role": "development",
                    "content_description": f"Player {index} scores",
                    "target_emotion": "excited",
                    "target_emotional_intensity": 0.7,
                    "target_kinetic_energy": 0.6,
                    "desired_duration_sec": 2.0,
                    "continuity_from_previous": "next match event",
                    "source_segment_id": f"segment_{index:04d}",
                    "required_visible_subjects": [f"Player {index}"],
                }
                for index in range(1, 4)
            ]
        },
        6.0,
        2.0,
        video,
        {"accents_sec": [2.0, 4.0], "beats_sec": [2.0, 4.0]},
        1000,
    )


def test_failed_slot_can_be_repaired_without_expanding_for_a_segment_change() -> None:
    video = _video()
    slots = _slots(video)

    assert _repair_window_slot_ids(slots, {"slot_02"}, video) == {"slot_02"}


def test_repair_constraints_include_original_segment_and_its_memory() -> None:
    video = _video()
    constraints = _targeted_slot_constraints(_slots(video), {"slot_02"}, video)

    assert constraints["slot_02"]["allowed_segment_ids"] == ["segment_0002"]
    assert constraints["slot_02"]["allowed_source_segments"] == [video["segments"][1]]
    assert "must_change_segment" not in constraints["slot_02"]


@pytest.mark.parametrize("legacy_binding", [False, True])
def test_real_redesign_path_accepts_new_content_and_subjects_on_original_segment(
    legacy_binding: bool,
) -> None:
    video = _video()
    slots = _slots(video)
    original = deepcopy(slots)
    failure = {
        "slot_id": "slot_02",
        "group_id": "group_002",
        "source_segment_id": "segment_0002",
        "reason_code": "required_subject_not_visually_confirmed",
        "content_description": "Player 2 scores",
        "required_visible_subjects": ["Player 2"],
        "candidate_rejections": [
            {
                "candidate_id": "candidate_failed",
                "timestamp": "00:00:11,000-00:00:13,000",
                "visual_evidence": "A header enters the net; the player's identity is unclear.",
                "required_visible_subjects": ["Player 2"],
                "reason_code": "required_subject_not_visually_confirmed",
            }
        ],
    }
    reply = {
        "slots": [
            {
                **{
                    key: value
                    for key, value in slots[1].items()
                    if key
                    in {
                        "slot_id", "narrative_role", "content_description",
                        "target_emotion", "target_emotional_intensity",
                        "target_kinetic_energy", "desired_duration_sec",
                        "planned_duration_ms", "continuity_from_previous",
                        "source_segment_id", "required_visible_subjects",
                    }
                },
                "content_description": "The attacking team scores a header from a corner",
                "required_visible_subjects": ["Attacking team"],
                "narrative_role": "climax",
                "target_emotion": "triumphant",
            }
        ]
    }

    class Context:
        def __init__(self) -> None:
            self.artifacts = {"video_description": video, "video_summary": {}}
            if legacy_binding:
                self.artifacts["planners_feedback"] = {
                    "forbidden_group_segment_bindings": [
                        {
                            "parent_group_id": "group_002",
                            "slot_ids": ["slot_02"],
                            "source_segment_id": "segment_0002",
                        }
                    ],
                    "unavailable_source_segment_ids": [],
                }
            self.calls = 0

        def get_artifact(self, name):
            return self.artifacts.get(name)

        def set_artifact(self, name, value):
            self.artifacts[name] = value

        def call_prompt(self, *, package, config, validate_business):
            self.calls += 1
            assert "A header enters the net" in package.user_prompt
            assert "Source evidence for match event 2" in package.user_prompt
            return validate_business(reply)

    context = Context()
    revised, repaired_ids = redesign_edit_slots(
        slots,
        [failure],
        LLMConfig(model="test", base_url="", api_key="test"),
        context,
    )

    assert context.calls == 1
    assert repaired_ids == {"slot_02"}
    assert revised[1]["source_segment_id"] == "segment_0002"
    assert revised[1]["required_visible_subjects"] == ["Attacking team"]
    assert revised[1]["narrative_role"] == "climax"
    assert revised[0] == original[0]
    assert revised[2] == original[2]
    assert slots == original


def test_confirmed_static_source_is_still_excluded_from_repair_choices() -> None:
    video = _video()
    slots = _slots(video)
    expanded = _repair_window_slot_ids(
        slots, {"slot_02"}, video, unavailable_source_segment_ids={"segment_0002"}
    )
    constraints = _targeted_slot_constraints(
        slots, expanded, video, unavailable_source_segment_ids={"segment_0002"}
    )

    assert "slot_02" in expanded
    assert all(
        "segment_0002" not in value["allowed_segment_ids"]
        for value in constraints.values()
    )
