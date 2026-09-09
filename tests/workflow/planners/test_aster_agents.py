from types import SimpleNamespace

import numpy as np
import pytest

from cutmaster.configuration.schema import (
    CandidateRetrievalConfig,
    LLMConfig,
    VLMConfig,
)
from cutmaster.workflow.planners.timeline_scout import (
    _candidate_motion,
    _candidate_segment_video_descriptions,
    _validate_visual_grounding,
    retrieve_candidates,
)
from cutmaster.workflow.planners.aster_team import ASTERTeam
from cutmaster.workflow.planners.edit_composer import (
    score_unary_candidate,
)
from cutmaster.workflow.planners.arrangement_architect import (
    _expand_target_group_slot_ids,
    _globally_align_boundaries,
    _source_story_context,
    _targeted_slot_constraints,
    _validate_targeted_slots,
    _validate_slots,
    align_slots_to_music,
)
from cutmaster.workflow.shared.execution_context import WorkflowContext

from test_partial_replan import (
    _planning_group as _reuse_group,
    _planning_segment as _reuse_segment,
    _slot as _reuse_slot,
    _trajectory as _reuse_trajectory,
)


def test_same_segment_semantic_repair_preserves_anchor_and_prior_segment_evidence(
    tmp_path,
) -> None:
    anchor = {"anchor_id": "dialogue_01", "source_segment_id": "segment_0002"}
    runtime_slots = [
        {
            **_reuse_slot(1),
            "group_id": "group_001_anchor_01",
            "source_segment_id": "segment_0002",
            "dialogue_anchor": anchor,
        },
        {
            **_reuse_slot(2),
            "group_id": "group_001_01",
            "parent_group_id": "group_001",
        },
    ]
    history = [
        {
            "slot_ids": ["slot_02"],
            "source_segment_id": segment,
            "reason_code": "required_subject_not_visually_confirmed",
        }
        for segment in ("segment_0001", "segment_0002")
    ]
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact(
        "planners_feedback",
        {
            "candidate_failure_evidence": [
                *history,
                {
                    "slot_ids": ["slot_99"],
                    "source_segment_id": "segment_0002",
                    "reason_code": "unrelated_failure",
                },
            ],
        },
    )
    diagnostics = {
        "failed_group_ids": ["group_001_01"],
        "failed_parent_group_ids": ["group_001"],
        "failed_slot_ids": ["slot_02"],
        "reason_code": "required_subject_not_visually_confirmed",
        "valid_trajectory_counts": {"group_001_01": 0},
    }
    calls = []

    def repair(received, failures):
        calls.append(failures)
        assert received[0]["dialogue_anchor"] == anchor
        assert failures[0]["candidate_failure_evidence"] == history
        assert failures[0]["diagnostics"] == diagnostics
        return (
            [received[0], {**received[1], "content_description": "visible action"}],
            {"slot_02"},
        )

    team = ASTERTeam.__new__(ASTERTeam)
    team.context = context
    team.story_editor = SimpleNamespace(
        restore_arrangement_slots=lambda received: [
            {
                key: value
                for key, value in {**slot, "group_id": "group_001"}.items()
                if key != "dialogue_anchor"
            }
            for slot in received
        ]
    )
    team.arrangement_architect = SimpleNamespace(repair=repair)

    repaired, replanned = team.repair_groups(runtime_slots, diagnostics)

    assert len(calls) == 1
    assert replanned == {"slot_02"}
    assert repaired[0]["dialogue_anchor"] == anchor
    assert repaired[1]["source_segment_id"] == "segment_0002"


@pytest.mark.parametrize(
    "revision", ["content", "subjects", "empty_pool", "failed_pool"]
)
def test_same_segment_retry_retrieves_only_changed_or_failed_groups(
    tmp_path,
    revision,
) -> None:
    previous_slots = [_reuse_slot(1), _reuse_slot(2)]
    slots = [dict(slot) for slot in previous_slots]
    if revision == "content":
        slots[1]["content_description"] = "another supported event"
    elif revision == "subjects":
        slots[1]["required_visible_subjects"] = ["visible teammate"]
    groups = [_reuse_group(1), _reuse_group(2)]
    segments = [_reuse_segment(1), _reuse_segment(2)]
    previous_pool = {
        "group_001": [_reuse_trajectory(1, suffix="old")],
        "group_002": (
            [] if revision == "empty_pool" else [_reuse_trajectory(2, suffix="old")]
        ),
    }
    new_trajectory = _reuse_trajectory(2, suffix="new")
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("planning_groups", groups)
    context.set_artifact("planning_segments", segments)
    if revision in {"empty_pool", "failed_pool"}:
        context.set_artifact(
            "planners_feedback",
            {"diagnostics": {
                "failed_group_ids": ["group_002"],
                "failed_parent_group_ids": ["group_002"],
            }},
        )
    calls = []

    def scout(
        received,
        _cancellation_token=None,
        *,
        target_group_ids,
        seed_candidate_pool,
    ):
        calls.append(target_group_ids)
        assert received == slots
        assert target_group_ids == {"group_002"}
        assert seed_candidate_pool == {"group_001": previous_pool["group_001"]}
        return {"group_002": [new_trajectory]}

    team = ASTERTeam.__new__(ASTERTeam)
    team.context = context
    team.timeline_scout = SimpleNamespace(scout=scout)
    team.config = SimpleNamespace(
        planners=SimpleNamespace(
            candidate_retrieval=SimpleNamespace(target_trajectories_per_group=3),
        )
    )
    team.validate_planning = lambda _slots: None

    result = team.scout_with_reuse(
        slots,
        previous_candidate_pool=previous_pool,
        previous_slots=previous_slots,
        previous_planning_groups=groups,
        previous_planning_segments=segments,
        affected_parent_group_ids={"group_001", "group_002"},
    )

    assert calls == [{"group_002"}]
    assert result == {
        "group_001": previous_pool["group_001"],
        "group_002": [new_trajectory],
    }


def _slots():
    return [
        {
            "slot_id": "slot_01",
            "content_description": "setup",
            "target_emotional_intensity": 0.2,
            "target_kinetic_energy": 0.2,
            "desired_duration_sec": 5,
        },
        {
            "slot_id": "slot_02",
            "content_description": "climax",
            "target_emotional_intensity": 0.9,
            "target_kinetic_energy": 0.9,
            "desired_duration_sec": 3,
        },
    ]


def _video_description():
    return {
        "segments": [
            {
                "segment_id": f"segment_{index:04d}",
                "time_range": {
                    "start_sec": float((index - 1) * 10),
                    "end_sec": float(index * 10),
                },
                "shots": [
                    {
                        "shot_id": f"shot_{index:05d}",
                        "time_range": {
                            "start_sec": float((index - 1) * 10),
                            "end_sec": float(index * 10),
                        },
                    }
                ],
            }
            for index in range(1, 4)
        ]
    }


def test_aster_team_warns_when_visual_shot_annotations_are_missing(
    monkeypatch,
) -> None:
    events: list[dict] = []
    video_description = _video_description()
    video_description["segments"][0]["shots"][0].update(
        {
            "visual_annotation_status": "provider_rejected",
            "visual_annotation_failure": "data_inspection_failed",
        }
    )
    aster_team = ASTERTeam.__new__(ASTERTeam)
    aster_team.context = SimpleNamespace(
        get_artifact=lambda name: (
            video_description if name == "video_description" else None
        )
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.aster_team.log_event",
        lambda _level, _component, _event, _message, **fields: events.append(
            fields
        ),
    )

    aster_team._warn_about_missing_shot_annotations()

    assert len(events) == 1
    assert events[0]["reason_code"] == "provider_data_inspection_failed"
    assert events[0]["diagnosis"]
    assert events[0]["repair_requirement"]
    assert events[0]["missing_shots"] == 1
    assert events[0]["missing_shot_count"] == 1
    assert events[0]["missing_shot_ids_preview"] == ["shot_00001"]


def test_slot_arrangement_story_context_excludes_shot_descriptions() -> None:
    video_description = {
        "source": {"title": "Example"},
        "segments": [
            {
                "segment_id": "segment_0001",
                "time_range": {"start_sec": 0.0, "end_sec": 10.0},
                "has_dialogue": False,
                "speech_mode": "none",
                "content_type": "narrative",
                "timeline_role": "opening",
                "segment_summary": "A woman enters a café.",
                "narrative_function": "Introduces the protagonist.",
                "emotional_tone": "hopeful",
                "emotional_intensity": 0.4,
                "appearing_characters": ["Mia"],
                "shots": [
                    {
                        "shot_id": "shot_00001",
                        "visual_description": "A verbose Shot-level description.",
                    }
                ],
            }
        ],
    }

    result = _source_story_context(
        video_description,
        {"synopsis": "Mia pursues her dream."},
    )

    assert "shots" not in result["segments"][0]
    assert result["segments"][0]["segment_summary"] == "A woman enters a café."


def test_candidate_visual_context_includes_segment_and_overlapping_shots() -> None:
    video_description = _video_description()
    segment = video_description["segments"][0]
    segment["segment_summary"] = "The focal subject enters the arena."
    segment["appearing_characters"] = [{"name": "focal subject"}]
    segment["shots"].append(
        {
            "shot_id": "shot_outside",
            "time_range": {"start_sec": 20.0, "end_sec": 24.0},
            "visual_description": "A different scene",
        }
    )
    segment["shots"][0]["visual_description"] = "The focal subject is visible."
    segment["shots"][0]["characters"] = [
        {
            "name": "focal subject",
            "identity_likert": 4,
            "identity_evidence": "A readable name is visible.",
        }
    ]
    segment["shots"][0]["dialogue"] = [
        {
            "dialogue_id": 1,
            "time_range": {"start_sec": 2.0, "end_sec": 4.0},
            "speaker": "Speaker 1",
            "text": "Here comes the focal subject.",
            "speech_mode": "monologue",
        },
        {
            "dialogue_id": 2,
            "time_range": {"start_sec": 7.0, "end_sec": 8.0},
            "speaker": "Speaker 1",
            "text": "This line is outside the candidate.",
            "speech_mode": "monologue",
        },
    ]
    candidate = {
        "timestamp": "00:00:01,000-00:00:05,000",
        "source_segment_id": "segment_0001",
    }

    contexts = _candidate_segment_video_descriptions(
        candidate,
        video_description,
    )

    assert contexts[0]["segment_summary"] == "The focal subject enters the arena."
    assert contexts[0]["appearing_characters"] == [{"name": "focal subject"}]
    assert [item["dialogue_id"] for item in contexts[0]["candidate_dialogue"]] == [1]
    assert [shot["shot_id"] for shot in contexts[0]["overlapping_shots"]] == [
        "shot_00001"
    ]
    assert contexts[0]["overlapping_shots"][0]["characters"][0][
        "identity_evidence"
    ] == "A readable name is visible."


def test_slot_validation_and_accent_alignment() -> None:
    raw = {
        "slots": [
            {
                "narrative_role": "setup",
                "content_description": "setup",
                "target_emotion": "hopeful",
                "target_emotional_intensity": 0.2,
                "target_kinetic_energy": 0.2,
                "desired_duration_sec": 5,
                "continuity_from_previous": "opening",
                "source_segment_id": "segment_0001",
                "required_visible_subjects": [],
            },
            {
                "narrative_role": "climax",
                "content_description": "climax",
                "target_emotion": "excited",
                "target_emotional_intensity": 0.9,
                "target_kinetic_energy": 0.9,
                "desired_duration_sec": 3,
                "continuity_from_previous": "rising action",
                "source_segment_id": "segment_0002",
                "required_visible_subjects": [],
            },
        ]
    }
    slots = _validate_slots(
        raw,
        8.0,
        4.0,
        _video_description(),
    )
    aligned = align_slots_to_music(
        slots,
        {"accents_sec": [5.0], "beats_sec": [4.0, 5.0]},
        8.0,
        30,
        4.0,
    )
    assert aligned[0]["output_end_sec"] == 5.0
    assert aligned[0]["planned_duration_ms"] == 5000
    assert sum(slot["planned_duration_sec"] for slot in aligned) == 8.0


def test_slot_validation_allows_a_segment_exactly_as_long_as_the_clip() -> None:
    video_description = _video_description()
    video_description["segments"][0]["time_range"]["end_sec"] = 5.0
    raw = {
        "slots": [
            {
                "narrative_role": "setup",
                "content_description": "setup",
                "target_emotion": "hopeful",
                "target_emotional_intensity": 0.2,
                "target_kinetic_energy": 0.2,
                "desired_duration_sec": 5.0,
                "continuity_from_previous": "opening",
                "source_segment_id": "segment_0001",
                "required_visible_subjects": [],
            }
        ]
    }

    slots = _validate_slots(
        raw,
        5.0,
        5.0,
        video_description,
    )

    assert slots[0]["source_segment_id"] == "segment_0001"


def test_slot_validation_allows_adjacent_slots_to_share_one_segment() -> None:
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
                "source_segment_id": "segment_0001",
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
                "source_segment_id": "segment_0001",
                "required_visible_subjects": [],
            },
        ]
    }

    slots = _validate_slots(
        raw,
        8.0,
        4.0,
        _video_description(),
    )

    assert [slot["group_id"] for slot in slots] == ["group_001", "group_001"]


def test_slot_validation_accepts_a_valid_source_segment() -> None:
    raw = {
        "slots": [
            {
                "content_description": "The focal subject starts a journey",
                "narrative_role": "setup",
                "target_emotion": "hopeful",
                "target_emotional_intensity": 0.5,
                "target_kinetic_energy": 0.5,
                "desired_duration_sec": 4,
                "continuity_from_previous": "opening",
                "source_segment_id": "segment_0001",
                "required_visible_subjects": ["focal subject"],
            }
        ]
    }
    slots = _validate_slots(
        raw,
        4.0,
        4.0,
        _video_description(),
    )

    assert slots[0]["source_segment_id"] == "segment_0001"


def test_slot_validation_accepts_model_selected_slot_count() -> None:
    raw = {
        "slots": [
            {
                "narrative_role": "setup",
                "content_description": "The focal subject begins",
                "target_emotion": "hopeful",
                "target_emotional_intensity": 0.4,
                "target_kinetic_energy": 0.3,
                "desired_duration_sec": 3.0,
                "continuity_from_previous": "opening",
                "source_segment_id": "segment_0001",
                "required_visible_subjects": ["focal subject"],
            },
            {
                "narrative_role": "resolution",
                "content_description": "The focal subject succeeds",
                "target_emotion": "fulfilled",
                "target_emotional_intensity": 0.7,
                "target_kinetic_energy": 0.4,
                "desired_duration_sec": 5.0,
                "continuity_from_previous": "resolves the journey",
                "source_segment_id": "segment_0002",
                "required_visible_subjects": ["focal subject"],
            },
        ]
    }

    slots = _validate_slots(
        raw,
        8.0,
        4.0,
        _video_description(),
    )

    assert len(slots) == 2
    assert sum(slot["desired_duration_sec"] for slot in slots) == 8.0


def test_slot_validation_rejects_duration_total_far_from_target() -> None:
    raw = {
        "slots": [
            {
                "narrative_role": "setup",
                "content_description": "The focal subject begins",
                "target_emotion": "hopeful",
                "target_emotional_intensity": 0.4,
                "target_kinetic_energy": 0.3,
                "desired_duration_sec": 4.0,
                "continuity_from_previous": "opening",
                "source_segment_id": "segment_0001",
                "required_visible_subjects": ["focal subject"],
            }
        ]
    }

    with pytest.raises(ValueError, match="must total"):
        _validate_slots(
            raw,
            8.0,
            4.0,
            _video_description(),
        )


def test_slot_validation_rejects_long_visual_slot_for_dialogue() -> None:
    raw = {
        "slots": [
            {
                "narrative_role": "development",
                "content_description": "A long exchange begins on synchronized lips",
                "target_emotion": "urgent",
                "target_emotional_intensity": 0.7,
                "target_kinetic_energy": 0.4,
                "desired_duration_sec": 8.0,
                "continuity_from_previous": "opening",
                "source_segment_id": "segment_0001",
                "required_visible_subjects": ["focal subject"],
            }
        ]
    }

    with pytest.raises(ValueError, match="start-aligned L-cut"):
        _validate_slots(
            raw,
            8.0,
            4.0,
            _video_description(),
        )


def test_slot_validation_rejects_average_far_from_visual_target() -> None:
    raw = {
        "slots": [
            {
                "narrative_role": "development",
                "content_description": f"Story beat {index}",
                "target_emotion": "urgent",
                "target_emotional_intensity": 0.7,
                "target_kinetic_energy": 0.4,
                "desired_duration_sec": 6.0,
                "continuity_from_previous": "continues",
                "source_segment_id": f"segment_{index:04d}",
                "required_visible_subjects": [],
            }
            for index in range(1, 3)
        ]
    }

    with pytest.raises(ValueError, match="Average Slot duration"):
        _validate_slots(
            raw,
            12.0,
            4.0,
            _video_description(),
        )


def test_slot_average_allows_integer_count_quantization_for_short_output() -> None:
    raw = {
        "slots": [
            {
                "narrative_role": "development",
                "content_description": f"Short-output beat {index}",
                "target_emotion": "focused",
                "target_emotional_intensity": 0.5,
                "target_kinetic_energy": 0.5,
                "desired_duration_sec": 10.0 / 3.0,
                "continuity_from_previous": "continues",
                "source_segment_id": f"segment_{index:04d}",
                "required_visible_subjects": [],
            }
            for index in range(1, 4)
        ]
    }

    slots = _validate_slots(
        raw,
        10.0,
        4.0,
        _video_description(),
    )

    assert len(slots) == 3


def test_candidate_retrieval_does_not_request_replacements_for_fixed_anchor(
    tmp_path,
    monkeypatch,
) -> None:
    fixed = {
        "candidate_id": "slot_01_dialogue_anchor",
        "slot_id": "slot_01",
        "source_segment_id": "segment_0001",
        "timestamp": "00:00:01,000-00:00:05,000",
        "semantic_relevance": 0.8,
        "emotional_intensity": 0.5,
        "salience": 1.0,
        "protagonist_visibility_likert": 3,
        "visual_slot_relevance_likert": 4,
        "kinetic_energy": 0.2,
    }
    slots = [
        {
            "slot_id": "slot_01",
            "group_id": "group_001_anchor_01",
            "parent_group_id": "group_001",
            "source_segment_id": "segment_0001",
            "planned_duration_ms": 4000,
            "planned_duration_sec": 4.0,
            "dialogue_anchor": {
                "source_segment_id": "segment_0001",
                "source_video_timestamp": "00:00:01,000-00:00:05,000",
            },
            "fixed_candidate": fixed,
        }
    ]
    context = WorkflowContext(tmp_path / "planners_history.json")
    context.set_artifact("video_description", _video_description())
    context.set_artifact("planning_groups", [])
    context.set_artifact("planning_segments", [])

    pool = retrieve_candidates(
        slots,
        object(),
        LLMConfig(model="unused", base_url="", api_key="unused"),
        VLMConfig(model="unused", base_url="", api_key="unused"),
        CandidateRetrievalConfig(target_trajectories_per_group=3),
        context,
    )

    assert pool == {}


def test_global_alignment_does_not_exhaust_later_accents() -> None:
    result = _globally_align_boundaries(
        [4.0, 8.0, 12.0],
        [3.8, 4.2, 7.9, 8.1, 11.9, 12.1],
        16.0,
        30,
    )
    assert len(result) == 3
    assert result == sorted(result)


def test_music_alignment_keeps_each_boundary_adjustment_local() -> None:
    slots = [
        {
            "slot_id": "slot_01",
            "desired_duration_sec": 4.0,
        },
        {
            "slot_id": "slot_02",
            "desired_duration_sec": 4.0,
        },
    ]

    aligned = align_slots_to_music(
        slots,
        {"accents_sec": [1.0, 7.0], "beats_sec": [1.0, 7.0]},
        8.0,
        30,
        4.0,
    )

    assert aligned[0]["output_end_sec"] == 4.0
    assert aligned[1]["output_end_sec"] == 8.0


def test_candidate_motion_trims_one_transient_frame_difference() -> None:
    class FakeMedia:
        @staticmethod
        def sample_frames(_sample_times):
            frames = []
            for index in range(20):
                if index < 14:
                    level = 0
                elif index == 14:
                    level = 6
                else:
                    level = 3
                frames.append(np.full((90, 160, 3), level, dtype="uint8"))
            return frames

    score = _candidate_motion(FakeMedia(), 0.0, 4.0, 5.0)

    assert score == pytest.approx(0.00363, abs=1e-5)


def test_candidate_motion_keeps_sustained_low_motion() -> None:
    class FakeMedia:
        @staticmethod
        def sample_frames(_sample_times):
            return [np.full((90, 160, 3), index, dtype="uint8") for index in range(20)]

    score = _candidate_motion(FakeMedia(), 0.0, 4.0, 5.0)

    assert score == pytest.approx(1.0 / 255.0 / 0.18)


def test_candidate_motion_does_not_trim_a_too_short_clip() -> None:
    class FakeMedia:
        @staticmethod
        def sample_frames(_sample_times):
            return [
                np.full((90, 160, 3), level, dtype="uint8")
                for level in (0, 1, 4)
            ]

    score = _candidate_motion(FakeMedia(), 0.0, 1.5, 2.0)

    assert score == pytest.approx(2.0 / 255.0 / 0.18)


def test_targeted_slot_replan_is_bounded_by_neighboring_fixed_slots() -> None:
    slots = [
        {
            "slot_id": f"slot_{index:02d}",
            "narrative_role": "development",
            "content_description": f"event {index}",
            "target_emotion": "focused",
            "target_emotional_intensity": 0.5,
            "target_kinetic_energy": 0.5,
            "desired_duration_sec": 4.0,
            "planned_duration_ms": 4000,
            "planned_duration_sec": 4.0,
            "output_start_sec": float((index - 1) * 4),
            "output_end_sec": float(index * 4),
            "continuity_from_previous": "continues",
            "source_segment_id": f"segment_{index:04d}",
            "group_id": f"group_{index:03d}",
            "required_visible_subjects": [],
        }
        for index in range(1, 4)
    ]
    constraints = _targeted_slot_constraints(
        slots,
        {"slot_02"},
        _video_description(),
    )

    assert constraints["slot_02"]["allowed_segment_ids"] == [
        "segment_0002",
    ]
    assert constraints["slot_02"]["previous_fixed_slot"]["slot_id"] == "slot_01"
    assert constraints["slot_02"]["next_fixed_slot"]["slot_id"] == "slot_03"

    replacement = {
        "slots": [
            {
                "slot_id": "slot_02",
                "narrative_role": "turning_point",
                "content_description": "a newly supported event",
                "target_emotion": "hopeful",
                "target_emotional_intensity": 0.7,
                "target_kinetic_energy": 0.6,
                "desired_duration_sec": 4.0,
                "planned_duration_ms": 4000,
                "planned_duration_sec": 4.0,
                "continuity_from_previous": "continues",
                "source_segment_id": "segment_0002",
                "required_visible_subjects": ["focal subject"],
            }
        ]
    }
    redesigned = _validate_targeted_slots(
        replacement,
        slots,
        constraints,
        _video_description(),
    )

    assert redesigned[0] == slots[0]
    assert redesigned[2] == slots[2]
    assert redesigned[1]["content_description"] == "a newly supported event"
    assert redesigned[1]["planned_duration_sec"] == 4.0
    assert redesigned[1]["output_start_sec"] == 4.0
    assert redesigned[1]["output_end_sec"] == 8.0

    replacement["slots"][0]["source_segment_id"] = "segment_0004"
    with pytest.raises(ValueError, match="outside its chronological interval"):
        _validate_targeted_slots(
            replacement,
            slots,
            constraints,
            _video_description(),
        )
    replacement["slots"][0]["source_segment_id"] = "segment_0002"
    replacement["slots"][0]["planned_duration_ms"] = 4100
    with pytest.raises(ValueError, match="changed planned duration"):
        _validate_targeted_slots(
            replacement,
            slots,
            constraints,
            _video_description(),
        )


def test_targeted_retry_expands_to_the_complete_source_group() -> None:
    slots = [
        {
            "slot_id": f"slot_{index:02d}",
            "content_description": f"event {index}",
            "desired_duration_sec": 4.0,
            "planned_duration_ms": 4000,
            "planned_duration_sec": 4.0,
            "source_segment_id": (
                "segment_0001" if index < 3 else "segment_0002"
            ),
            "group_id": "group_001" if index < 3 else "group_002",
        }
        for index in range(1, 4)
    ]

    expanded_slot_ids = _expand_target_group_slot_ids(
        slots,
        {"slot_02"},
    )
    constraints = _targeted_slot_constraints(
        slots,
        expanded_slot_ids,
        _video_description(),
    )

    assert expanded_slot_ids == {"slot_01", "slot_02"}
    assert all(
        constraint["allowed_segment_ids"]
        == ["segment_0001"]
        for constraint in constraints.values()
    )


def test_targeted_retry_keeps_an_anchored_group_atomic() -> None:
    slots = [
        {
            "slot_id": f"slot_{index:02d}",
            "content_description": f"event {index}",
            "desired_duration_sec": 4.0,
            "planned_duration_ms": 4000,
            "planned_duration_sec": 4.0,
            "source_segment_id": "segment_0001",
            "group_id": "group_001",
            **(
                {"fixed_candidate": {"candidate_id": f"anchor_{index}"}}
                if index == 1
                else {}
            ),
        }
        for index in range(1, 4)
    ]

    expanded_slot_ids = _expand_target_group_slot_ids(
        slots,
        {"slot_02"},
    )

    assert expanded_slot_ids == {"slot_01", "slot_02", "slot_03"}


def test_visual_grounding_requires_integer_likert_scores() -> None:
    candidates = [{"candidate_id": "candidate_01"}]
    response = {
        "items": [
            {
                "candidate_id": "candidate_01",
                "visible_description": "the focal subject is visible",
                "visible_subjects": ["focal subject"],
                "required_subject_visibility": 4,
                "visual_slot_relevance": 4,
                "visual_evidence": "clear face in multiple frames",
            }
        ]
    }
    result = _validate_visual_grounding(response, candidates)
    assert result["candidate_01"]["protagonist_visibility_likert"] == 4
    assert result["candidate_01"]["visual_slot_relevance_likert"] == 4

    response["items"][0]["required_subject_visibility"] = 0.75
    with pytest.raises(ValueError, match="must be 1 to 5"):
        _validate_visual_grounding(response, candidates)
    response["items"][0]["required_subject_visibility"] = 4
    response["items"][0]["visual_slot_relevance"] = 0.8
    with pytest.raises(ValueError, match="visual_slot_relevance.*must be 1 to 5"):
        _validate_visual_grounding(response, candidates)


def test_visual_likert_scores_are_normalized_for_unary() -> None:
    slot = _slots()[0] | {
        "planned_duration_ms": 5000,
        "planned_duration_sec": 5.0,
        "required_visible_subjects": ["focal subject"],
    }
    base = {
        "timestamp": "00:00:01,000-00:00:06,000",
        "description": "woman at a cafe",
        "semantic_relevance": 0.9,
        "visual_slot_relevance_likert": 5,
        "emotional_intensity": 0.2,
        "kinetic_energy": 0.2,
        "salience": 0.8,
    }
    assert score_unary_candidate(
        slot, base | {"protagonist_visibility_likert": 5}
    ) > score_unary_candidate(
        slot, base | {"protagonist_visibility_likert": 1}
    )
    assert score_unary_candidate(
        slot, base | {"protagonist_visibility_likert": 5}
    ) > score_unary_candidate(
        slot,
        base
        | {
            "protagonist_visibility_likert": 5,
            "visual_slot_relevance_likert": 1,
        },
    )


def test_unary_uses_requested_quality_weights() -> None:
    slot = _slots()[0] | {
        "planned_duration_ms": 5000,
        "planned_duration_sec": 5.0,
        "required_visible_subjects": ["focal subject"],
    }
    candidate = {
        "timestamp": "00:00:01,000-00:00:05,000",
        "semantic_relevance": 0.8,
        "visual_slot_relevance_likert": 4,
        "protagonist_visibility_likert": 3,
        "emotional_intensity": 0.4,
        "kinetic_energy": 0.5,
        "salience": 0.9,
    }
    assert score_unary_candidate(slot, candidate) == pytest.approx(0.73)
