import json
import re
import threading
from types import SimpleNamespace

import numpy as np
import pytest

from cutmaster.configuration.schema import (
    CandidateRetrievalConfig,
    LLMConfig,
    VLMConfig,
)
from cutmaster.workflow.prompting.failure_catalog import (
    PromptFailureCode,
    build_prompt_failure,
)
from cutmaster.workflow.planners.timeline_scout import (
    _candidate_motion,
    _candidate_segment_video_descriptions,
    _retrieval_segment_context,
    _validate_candidates,
    _validate_visual_grounding,
    _usable_segment_count,
    retrieve_candidates,
)
from cutmaster.workflow.planners.revision_editor import review_and_patch
from cutmaster.workflow.planners.aster_team import ASTERTeam
from cutmaster.workflow.planners.edit_composer import (
    EditComposerAgent,
    NoFeasiblePathError,
    _pair_key,
    score_unary_candidate,
    path_to_script,
    select_paths,
    validate_chronological_path,
    validate_source_segment_availability,
)
from cutmaster.workflow.planners.arrangement_architect import (
    _globally_align_boundaries,
    _source_story_context,
    _targeted_slot_constraints,
    _validate_targeted_slots,
    _validate_slots,
    align_slots_to_music,
)
from cutmaster.workflow.shared.execution_context import WorkflowContext


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
        "source_segment_ids": ["segment_0001"],
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
                "source_segment_ids": ["segment_0001"],
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
        {},
    )
    aligned = align_slots_to_music(
        slots,
        {"accents_sec": [5.0], "beats_sec": [4.0, 5.0]},
        8.0,
        30,
        4.0,
    )
    assert aligned[0]["output_end_sec"] == 5.0
    assert sum(slot["planned_duration_sec"] for slot in aligned) == 8.0


def test_slot_validation_requires_one_segment_longer_than_the_clip() -> None:
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
                "source_segment_ids": ["segment_0001"],
                "required_visible_subjects": [],
            }
        ]
    }

    with pytest.raises(ValueError, match="source Segment longer than"):
        _validate_slots(
            raw,
            5.0,
            5.0,
            video_description,
            set(),
            {},
        )


def test_slot_validation_requires_strictly_increasing_segment_ranges() -> None:
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
                "source_segment_ids": ["segment_0001"],
                "required_visible_subjects": [],
            },
        ]
    }

    with pytest.raises(ValueError, match="strictly increasing source order"):
        _validate_slots(
            raw,
            8.0,
            4.0,
            _video_description(),
            set(),
            {},
        )


def test_slot_validation_rejects_failed_replan_assignments() -> None:
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
                "source_segment_ids": ["segment_0001"],
                "required_visible_subjects": ["focal subject"],
            }
        ]
    }
    with pytest.raises(ValueError, match="visually disproven"):
        _validate_slots(
            raw,
            4.0,
            4.0,
            _video_description(),
            {"segment_0001"},
            {},
        )
    with pytest.raises(
        ValueError,
        match="slot_01.*attempted assignment.*forbidden assignments",
    ):
        _validate_slots(
            raw,
            4.0,
            4.0,
            _video_description(),
            set(),
            hard_forbidden_assignments={
                "slot_01": {("segment_0001",)},
            },
        )


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
                "source_segment_ids": ["segment_0001"],
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
                "source_segment_ids": ["segment_0002"],
                "required_visible_subjects": ["focal subject"],
            },
        ]
    }

    slots = _validate_slots(
        raw,
        8.0,
        4.0,
        _video_description(),
        set(),
        {},
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
                "source_segment_ids": ["segment_0001"],
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
            set(),
            {},
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
                "source_segment_ids": ["segment_0001"],
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
            set(),
            {},
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
                "source_segment_ids": [f"segment_{index:04d}"],
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
            set(),
            {},
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
                "source_segment_ids": [f"segment_{index:04d}"],
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
        set(),
        {},
    )

    assert len(slots) == 3


def test_candidate_retrieval_does_not_request_replacements_for_fixed_anchor(
    tmp_path,
    monkeypatch,
) -> None:
    fixed = {
        "candidate_id": "slot_01_dialogue_anchor",
        "slot_id": "slot_01",
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
            "planned_duration_sec": 4.0,
            "fixed_candidate": fixed,
        }
    ]
    context = WorkflowContext(tmp_path / "planners_history.json")
    context.set_artifact("video_description", _video_description())
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda *args, **kwargs: None,
    )

    pool = retrieve_candidates(
        slots,
        tmp_path / "unused.mp4",
        LLMConfig(model="unused", base_url="", api_key="unused"),
        VLMConfig(model="unused", base_url="", api_key="unused"),
        CandidateRetrievalConfig(candidates_per_slot=3),
        context,
    )

    assert pool == {"slot_01": [fixed]}


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


def test_chronology_preflight_accepts_a_complete_path() -> None:
    slots = _slots()
    for slot in slots:
        slot["planned_duration_sec"] = slot["desired_duration_sec"]
    pool = {
        "slot_01": [
            {"candidate_id": "a", "timestamp": "00:00:01,000-00:00:07,000", "description": "subject starts", "semantic_relevance": 1.0, "emotional_intensity": 0.2, "kinetic_energy": 0.2, "salience": 1.0},
            {"candidate_id": "b", "timestamp": "00:00:20,000-00:00:26,000", "description": "subject starts", "semantic_relevance": 0.95, "emotional_intensity": 0.2, "kinetic_energy": 0.2, "salience": 1.0},
        ],
        "slot_02": [
            {"candidate_id": "c", "timestamp": "00:00:10,000-00:00:14,000", "description": "subject climax", "semantic_relevance": 1.0, "emotional_intensity": 0.9, "kinetic_energy": 0.9, "salience": 1.0},
        ],
    }
    validate_chronological_path(slots, pool)


def test_composition_preflight_rejects_unavailable_source_segment() -> None:
    slots = _slots()
    pool = {
        "slot_01": [
            {
                "candidate_id": "static-candidate",
                "timestamp": "00:00:01,000-00:00:07,000",
                "source_segment_ids": ["segment_0099"],
            }
        ],
        "slot_02": [
            {
                "candidate_id": "valid-candidate",
                "timestamp": "00:00:10,000-00:00:14,000",
                "source_segment_ids": ["segment_0100"],
            }
        ],
    }

    with pytest.raises(NoFeasiblePathError) as exc_info:
        validate_source_segment_availability(
            slots,
            pool,
            {"segment_0099"},
        )

    assert exc_info.value.diagnostics == {
        "reason_code": "candidate_uses_unavailable_source_segment",
        "failed_slot_id": "slot_01",
        "failed_slot_ids": ["slot_01"],
        "candidate_ids": ["static-candidate"],
        "source_segment_ids": ["segment_0099"],
        "unavailable_source_segment_ids": ["segment_0099"],
    }


def test_composer_reads_unavailable_segments_from_restored_feedback(tmp_path) -> None:
    slots = _slots()
    pool = {
        "slot_01": [
            {
                "candidate_id": "legacy-static-candidate",
                "timestamp": "00:00:01,000-00:00:07,000",
                "source_segment_ids": ["segment_0099"],
            }
        ],
        "slot_02": [
            {
                "candidate_id": "valid-candidate",
                "timestamp": "00:00:10,000-00:00:14,000",
                "source_segment_ids": ["segment_0100"],
            }
        ],
    }
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact(
        "planners_feedback",
        {"unavailable_source_segment_ids": ["segment_0099"]},
    )
    composer = EditComposerAgent(
        None,
        tmp_path / "video.mp4",
        None,
        context,
    )

    with pytest.raises(NoFeasiblePathError) as exc_info:
        composer.validate(slots, pool)

    assert exc_info.value.diagnostics["candidate_ids"] == [
        "legacy-static-candidate"
    ]


def test_beam_search_rejects_source_time_reversal() -> None:
    slots = _slots()
    for slot in slots:
        slot["planned_duration_sec"] = slot["desired_duration_sec"]
    pool = {
        "slot_01": [
            {"candidate_id": "a", "timestamp": "00:00:20,000-00:00:26,000", "description": "first", "semantic_relevance": 1.0, "emotional_intensity": 0.2, "kinetic_energy": 0.2, "salience": 1.0},
        ],
        "slot_02": [
            {"candidate_id": "b", "timestamp": "00:00:10,000-00:00:14,000", "description": "second", "semantic_relevance": 1.0, "emotional_intensity": 0.9, "kinetic_energy": 0.9, "salience": 1.0},
        ],
    }
    with pytest.raises(NoFeasiblePathError, match="chronological"):
        validate_chronological_path(slots, pool)


def test_beam_search_scores_only_edges_from_surviving_ends(
    tmp_path,
    monkeypatch,
) -> None:
    slots = [
        {
            "slot_id": f"slot_{index:02d}",
            "content_description": f"scene {index}",
            "continuity_from_previous": "direct progression",
            "target_emotional_intensity": 0.5,
            "target_kinetic_energy": 0.5,
            "planned_duration_sec": 3.0,
        }
        for index in range(1, 4)
    ]
    shared = {
        "description": "focal subject",
        "semantic_relevance": 0.5,
        "visual_slot_relevance_likert": 5,
        "protagonist_visibility_likert": 5,
        "salience": 1.0,
        "emotional_intensity": 0.5,
        "kinetic_energy": 0.5,
    }
    pool = {
        "slot_01": [
            shared
            | {
                "candidate_id": "a",
                "slot_id": "slot_01",
                "timestamp": "00:00:01,000-00:00:04,000",
                "semantic_relevance": 1.0,
            },
            shared
            | {
                "candidate_id": "b",
                "slot_id": "slot_01",
                "timestamp": "00:00:05,000-00:00:08,000",
            },
        ],
        "slot_02": [
            shared
            | {
                "candidate_id": "c",
                "slot_id": "slot_02",
                "timestamp": "00:00:10,000-00:00:13,000",
                "semantic_relevance": 1.0,
            },
            shared
            | {
                "candidate_id": "d",
                "slot_id": "slot_02",
                "timestamp": "00:00:14,000-00:00:17,000",
            },
        ],
        "slot_03": [
            shared
            | {
                "candidate_id": "e",
                "slot_id": "slot_03",
                "timestamp": "00:00:20,000-00:00:23,000",
            }
        ],
    }
    scored_previous_ids: list[list[str]] = []

    def score_layer(
        media,
        previous_slot,
        current_slot,
        previous_candidates,
        current_candidates,
        config,
        context,
        *,
        sample_frames,
    ):
        scored_previous_ids.append(
            [candidate["candidate_id"] for candidate in previous_candidates]
        )
        return {
            _pair_key(previous["candidate_id"], current["candidate_id"]): {
                "pairwise_score": (
                    1.0 if current["candidate_id"] in {"c", "e"} else 0.0
                )
            }
            for previous in previous_candidates
            for current in current_candidates
        }

    monkeypatch.setattr(
        "cutmaster.workflow.planners.edit_composer._score_pairwise_layer",
        score_layer,
    )

    class Context:
        def __init__(self) -> None:
            self.artifacts = {}

        def set_artifact(self, key, value) -> None:
            self.artifacts[key] = value

    context = Context()
    beam, diagnostics, pairwise_scores = select_paths(
        tmp_path / "video.mp4",
        slots,
        pool,
        beam_width=1,
        config=VLMConfig(model="test", base_url="", api_key="test"),
        context=context,
        sample_frames=2,
    )
    assert [item["candidate_id"] for item in beam] == ["a", "c", "e"]
    assert scored_previous_ids == [["a"], ["c"]]
    assert set(pairwise_scores) == {
        _pair_key("a", "c"),
        _pair_key("a", "d"),
        _pair_key("c", "e"),
    }
    assert diagnostics["pairwise_scoring"] == (
        "lazy_vlm_hard_cut_from_surviving_beam_ends"
    )
    assert context.artifacts["pairwise_scores"] == pairwise_scores


def test_pairwise_vlm_scores_surviving_ends_in_parallel(
    tmp_path,
    monkeypatch,
) -> None:
    slots = [
        {
            "slot_id": f"slot_{index:02d}",
            "content_description": f"scene {index}",
            "continuity_from_previous": "direct progression",
            "target_kinetic_energy": 0.5,
        }
        for index in range(1, 3)
    ]
    pool = {
        "slot_01": [
            {
                "candidate_id": f"previous_{index}",
                "slot_id": "slot_01",
                "timestamp": f"00:00:0{index},000-00:00:05,000",
                "description": f"previous scene {index}",
                "semantic_relevance": 1.0,
                "visual_slot_relevance_likert": 5,
                "protagonist_visibility_likert": 5,
                "emotional_intensity": 0.5,
                "kinetic_energy": 0.5,
                "salience": 1.0,
            }
            for index in range(1, 3)
        ],
        "slot_02": [
            {
                "candidate_id": "current_1",
                "slot_id": "slot_02",
                "timestamp": "00:00:10,000-00:00:14,000",
                "description": "current scene",
                "semantic_relevance": 1.0,
                "visual_slot_relevance_likert": 5,
                "protagonist_visibility_likert": 5,
                "emotional_intensity": 0.5,
                "kinetic_energy": 0.5,
                "salience": 1.0,
            }
        ],
    }
    for slot in slots:
        slot["target_emotional_intensity"] = 0.5
        slot["planned_duration_sec"] = 4.0
    monkeypatch.setattr(
        "cutmaster.workflow.planners.edit_composer._edge_contact_sheet_data_url",
        lambda *args, **kwargs: "data:image/jpeg;base64,stub",
    )
    barrier = threading.Barrier(2)
    worker_ids: set[int] = set()
    worker_lock = threading.Lock()

    class Context:
        def __init__(self) -> None:
            self.artifacts = {}

        def call_prompt(self, **kwargs):
            with worker_lock:
                worker_ids.add(threading.get_ident())
            barrier.wait(timeout=2)
            previous_id = kwargs["image_labels"][0].split()[0]
            current_id = kwargs["image_labels"][1].split()[0]
            parsed = {
                "items": [
                    {
                        "previous_candidate_id": previous_id,
                        "current_candidate_id": current_id,
                        "visual_continuity": 0.8,
                        "emotional_continuity": 0.7,
                        "narrative_bridge": 0.6,
                        "evidence": "coherent hard cut",
                    }
                ]
            }
            kwargs["package"].response_contract.validate_structure(parsed)
            return kwargs["validate_business"](parsed)

        def set_artifact(self, key, value) -> None:
            self.artifacts[key] = value

    context = Context()
    _, _, scores = select_paths(
        tmp_path / "video.mp4",
        slots,
        pool,
        beam_width=2,
        config=VLMConfig(
            model="test",
            base_url="",
            api_key="test",
            max_concurrency=2,
        ),
        context=context,
        sample_frames=2,
    )
    assert len(worker_ids) == 2
    assert len(scores) == 2
    assert all(item["pairwise_score"] == pytest.approx(0.765) for item in scores.values())
    assert context.artifacts["pairwise_scores"] == scores


def test_current_slot_unary_and_pairwise_scoring_overlap(
    tmp_path,
    monkeypatch,
) -> None:
    slots = [
        {
            "slot_id": f"slot_{index:02d}",
            "target_emotional_intensity": 0.5,
            "target_kinetic_energy": 0.5,
            "planned_duration_sec": 3.0,
        }
        for index in range(1, 3)
    ]
    candidate = {
        "description": "visible scene",
        "semantic_relevance": 1.0,
        "visual_slot_relevance_likert": 5,
        "protagonist_visibility_likert": 5,
        "emotional_intensity": 0.5,
        "kinetic_energy": 0.5,
        "salience": 1.0,
    }
    pool = {
        "slot_01": [
            candidate
            | {
                "candidate_id": "a",
                "slot_id": "slot_01",
                "timestamp": "00:00:01,000-00:00:04,000",
            }
        ],
        "slot_02": [
            candidate
            | {
                "candidate_id": "b",
                "slot_id": "slot_02",
                "timestamp": "00:00:05,000-00:00:08,000",
            }
        ],
    }
    barrier = threading.Barrier(2)
    worker_ids: set[int] = set()
    worker_lock = threading.Lock()

    def score_unary(slot, candidates):
        if slot["slot_id"] == "slot_02":
            with worker_lock:
                worker_ids.add(threading.get_ident())
            barrier.wait(timeout=2)
        return {item["candidate_id"]: 1.0 for item in candidates}

    def score_pairwise(
        media,
        previous_slot,
        current_slot,
        previous_candidates,
        current_candidates,
        config,
        context,
        *,
        sample_frames,
    ):
        with worker_lock:
            worker_ids.add(threading.get_ident())
        barrier.wait(timeout=2)
        return {_pair_key("a", "b"): {"pairwise_score": 1.0}}

    monkeypatch.setattr(
        "cutmaster.workflow.planners.edit_composer._score_unary_candidates",
        score_unary,
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.edit_composer._score_pairwise_layer",
        score_pairwise,
    )

    class Context:
        def set_artifact(self, key, value) -> None:
            pass

    select_paths(
        tmp_path / "video.mp4",
        slots,
        pool,
        beam_width=1,
        config=VLMConfig(model="test", base_url="", api_key="test"),
        context=Context(),
        sample_frames=2,
    )
    assert len(worker_ids) == 2


def test_candidate_validation_requires_exact_duration_and_allows_overlap() -> None:
    slots = [_slots()[0] | {"planned_duration_sec": 5.0}]
    source_segments = {
        "slot_01": [
            {
                "segment_id": "segment_0001",
                "time_range": {"start_sec": 8.0, "end_sec": 20.0},
                "shots": [
                    {
                        "shot_id": "shot_00001",
                        "time_range": {"start_sec": 8.0, "end_sec": 14.0},
                    },
                    {
                        "shot_id": "shot_00002",
                        "time_range": {"start_sec": 14.0, "end_sec": 20.0},
                    }
                ],
            }
        ]
    }
    raw_item = {
        "timestamp": "00:00:08,000-00:00:13,000",
        "description": "structured visual context",
        "matched_dialogue": "hello",
        "semantic_relevance": 0.8,
        "emotional_intensity": 0.3,
        "salience": 0.7,
    }
    with pytest.raises(ValueError, match="exactly 2"):
        _validate_candidates(
            {"candidates": [{"slot_id": "slot_01", "items": [raw_item]}]},
            slots,
            source_segments,
            2,
        )
    result = _validate_candidates(
        {"candidates": [{"slot_id": "slot_01", "items": [raw_item]}]},
        slots,
        source_segments,
        1,
    )
    assert result["slot_01"][0]["timestamp"] == "00:00:08,000-00:00:13,000"
    assert result["slot_01"][0]["source_shot_ids"] == ["shot_00001"]
    assert result["slot_01"][0]["source_segment_ids"] == ["segment_0001"]

    overlapping_item = {
        **raw_item,
        "timestamp": "00:00:12,000-00:00:17,000",
    }
    overlapping = _validate_candidates(
        {
            "candidates": [
                {
                    "slot_id": "slot_01",
                    "items": [raw_item, overlapping_item],
                }
            ]
        },
        slots,
        source_segments,
        2,
    )
    assert len(overlapping["slot_01"]) == 2

    with pytest.raises(ValueError, match="duplicated"):
        _validate_candidates(
            {
                "candidates": [
                    {
                        "slot_id": "slot_01",
                        "items": [raw_item, raw_item],
                    }
                ]
            },
            slots,
            source_segments,
            2,
        )

    with pytest.raises(ValueError, match="duration must equal"):
        _validate_candidates(
            {
                "candidates": [
                    {
                        "slot_id": "slot_01",
                        "items": [
                            {
                                **raw_item,
                                "timestamp": "00:00:08,000-00:00:14,000",
                            }
                        ],
                    }
                ]
            },
            slots,
            source_segments,
            1,
        )


def test_usable_segment_count_only_requires_one_complete_displaceable_window() -> None:
    segments = [
        {
            "segment_id": "segment_0001",
            "time_range": {"start_sec": 0.0, "end_sec": 20.0},
            "shots": [],
        }
    ]

    assert _usable_segment_count(segments, 5.0) == 1
    assert _usable_segment_count(segments, 20.0) == 0


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
            "planned_duration_sec": 4.0,
            "output_start_sec": float((index - 1) * 4),
            "output_end_sec": float(index * 4),
            "continuity_from_previous": "continues",
            "source_segment_ids": [f"segment_{index:04d}"],
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
                "planned_duration_sec": 4.0,
                "continuity_from_previous": "continues",
                "source_segment_ids": ["segment_0002"],
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

    assert redesigned[0] is slots[0]
    assert redesigned[2] is slots[2]
    assert redesigned[1]["content_description"] == "a newly supported event"
    assert redesigned[1]["planned_duration_sec"] == 4.0
    assert redesigned[1]["output_start_sec"] == 4.0
    assert redesigned[1]["output_end_sec"] == 8.0

    replacement["slots"][0]["source_segment_ids"] = ["segment_0004"]
    with pytest.raises(ValueError, match="outside its chronological interval"):
        _validate_targeted_slots(
            replacement,
            slots,
            constraints,
            _video_description(),
        )
    replacement["slots"][0]["source_segment_ids"] = ["segment_0002"]
    replacement["slots"][0]["planned_duration_sec"] = 4.1
    with pytest.raises(ValueError, match="changed planned duration"):
        _validate_targeted_slots(
            replacement,
            slots,
            constraints,
            _video_description(),
        )


def test_replanned_anchor_segment_triggers_global_anchor_refresh(
    tmp_path,
    monkeypatch,
) -> None:
    original_slots = [
        {
            "slot_id": "slot_01",
            "source_segment_ids": ["segment_0001"],
            "dialogue_anchor": {"source_segment_id": "segment_0001"},
            "fixed_candidate": {"candidate_id": "old_anchor"},
        },
        {
            "slot_id": "slot_02",
            "source_segment_ids": ["segment_0003"],
        },
    ]
    redesigned_slots = [
        {
            **original_slots[0],
            "source_segment_ids": ["segment_0002"],
        },
        original_slots[1],
    ]
    refreshed_slots = [
        {
            **redesigned_slots[0],
            "dialogue_anchor": {"source_segment_id": "segment_0002"},
            "fixed_candidate": {"candidate_id": "new_anchor_1"},
        },
        {
            **redesigned_slots[1],
            "dialogue_anchor": {"source_segment_id": "segment_0003"},
            "fixed_candidate": {"candidate_id": "new_anchor_2"},
        },
    ]
    aster_team = ASTERTeam.__new__(ASTERTeam)
    aster_team.config = SimpleNamespace(
        llm=LLMConfig(model="test", base_url="", api_key="test")
    )
    aster_team.context = WorkflowContext(tmp_path / "history.json")
    refresh_calls: list[list[dict]] = []

    def refresh(slots):
        refresh_calls.append(slots)
        return refreshed_slots

    aster_team.arrangement_architect = SimpleNamespace(
        repair=lambda *_args: (redesigned_slots, {"slot_01"})
    )
    aster_team.story_editor = SimpleNamespace(anchor=refresh)
    result, reset_slot_ids = aster_team._redesign_slots_and_refresh_anchors(
        original_slots,
        [{"slot_id": "slot_01", "reason": "visually_static"}],
    )

    assert refresh_calls == [redesigned_slots]
    assert result == refreshed_slots
    assert reset_slot_ids == {"slot_01", "slot_02"}


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
    with pytest.raises(ValueError, match="integer from 1 to 5"):
        _validate_visual_grounding(response, candidates)
    response["items"][0]["required_subject_visibility"] = 4
    response["items"][0]["visual_slot_relevance"] = 0.8
    with pytest.raises(ValueError, match="visual_slot_relevance.*integer from 1 to 5"):
        _validate_visual_grounding(response, candidates)


def test_retrieval_context_expands_only_when_adjacent_scope_is_requested() -> None:
    slots = [{"slot_id": "slot_01", "source_segment_ids": ["segment_0002"]}]
    planned_scope = _retrieval_segment_context(
        _video_description(),
        slots,
        include_adjacent=False,
    )
    adjacent_scope = _retrieval_segment_context(
        _video_description(),
        slots,
        include_adjacent=True,
    )
    assert [
        segment["segment_id"] for segment in planned_scope["slot_01"]
    ] == ["segment_0002"]
    assert [
        segment["segment_id"] for segment in adjacent_scope["slot_01"]
    ] == ["segment_0001", "segment_0002", "segment_0003"]


def test_candidate_retrieval_accepts_distinct_shots_in_one_segment(
    tmp_path,
    monkeypatch,
) -> None:
    slot = {
        **_slots()[0],
        "planned_duration_sec": 4.0,
        "source_segment_ids": ["segment_0002"],
        "required_visible_subjects": [],
    }
    video_description = _video_description()
    video_description["segments"][1]["time_range"]["end_sec"] = 30.0
    video_description["segments"][1]["shots"] = [
        {
            "shot_id": f"shot_00002_{index}",
            "time_range": {"start_sec": start, "end_sec": end},
        }
        for index, (start, end) in enumerate(
            ((10.0, 16.0), (16.0, 22.0), (22.0, 30.0)),
            1,
        )
    ]
    video_description["segments"][2]["time_range"] = {
        "start_sec": 30.0,
        "end_sec": 40.0,
    }
    video_description["segments"][2]["shots"][0]["time_range"] = {
        "start_sec": 30.0,
        "end_sec": 40.0,
    }
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "test"})
    context.set_artifact("video_description", video_description)
    operations = []

    def call_prompt(**kwargs):
        package = kwargs["package"]
        operations.append(package.operation)
        response = {
            "candidates": [
                {
                    "slot_id": "slot_01",
                    "items": [
                        {
                            "timestamp": timestamp,
                            "description": "visible source content",
                            "matched_dialogue": "",
                            "semantic_relevance": 0.8,
                            "emotional_intensity": 0.5,
                            "salience": 0.7,
                        }
                        for timestamp in (
                            "00:00:10,000-00:00:14,000",
                            "00:00:16,000-00:00:20,000",
                            "00:00:22,000-00:00:26,000",
                        )
                    ],
                }
            ]
        }
        return kwargs["validate_business"](response)

    def add_visual_features(_video_path, slots, pool, *_args, **_kwargs):
        for candidate in pool[slots[0]["slot_id"]]:
            candidate.update(
                {
                    "description": "visible source content",
                    "visible_subjects": [],
                    "protagonist_visibility_likert": 2,
                    "visual_slot_relevance_likert": 4,
                    "visual_evidence": "sampled frames match",
                }
            )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        add_visual_features,
    )

    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda _video_path, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )

    pool = retrieve_candidates(
        [slot],
        tmp_path / "video.mp4",
        LLMConfig(model="text", base_url="", api_key="test"),
        VLMConfig(model="vision", base_url="", api_key="test"),
        CandidateRetrievalConfig(
            candidates_per_slot=3,
            retrieval_max_rounds=2,
        ),
        context,
    )

    assert operations == ["Candidate retrieval round 1 slot slot_01"]
    assert len(pool["slot_01"]) == 3


def test_candidate_retrieval_uses_four_planned_then_one_adjacent_round(
    tmp_path,
    monkeypatch,
) -> None:
    slot = {
        **_slots()[0],
        "planned_duration_sec": 4.0,
        "source_segment_ids": ["segment_0002"],
        "required_visible_subjects": [],
    }
    video_description = _video_description()
    video_description["segments"][1]["time_range"]["end_sec"] = 110.0
    video_description["segments"][1]["shots"][0]["time_range"]["end_sec"] = 110.0
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "test"})
    context.set_artifact("video_description", video_description)
    operations: list[str] = []
    adjacent_flags: list[bool] = []
    original_context_builder = _retrieval_segment_context

    def record_scope(video_description, slots, *, include_adjacent):
        adjacent_flags.append(include_adjacent)
        return original_context_builder(
            video_description,
            slots,
            include_adjacent=include_adjacent,
        )

    def fail_prompt(**kwargs):
        operations.append(kwargs["package"].operation)
        raise RuntimeError("synthetic retrieval failure")

    monkeypatch.setattr(context, "call_prompt", fail_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout._retrieval_segment_context",
        record_scope,
    )

    with pytest.raises(
        ValueError,
        match="No usable candidate remains after visual diagnostics",
    ):
        retrieve_candidates(
            [slot],
            tmp_path / "video.mp4",
            LLMConfig(model="text", base_url="", api_key="test"),
            VLMConfig(model="vision", base_url="", api_key="test"),
            CandidateRetrievalConfig(
                candidates_per_slot=3,
                retrieval_max_rounds=3,
            ),
            context,
        )

    assert operations == [
        f"Candidate retrieval round {round_index} slot slot_01"
        for round_index in range(1, 6)
    ]
    assert adjacent_flags == [False, False, False, False, True]


def test_vlm_rejection_retries_planned_segment_and_preserves_confirmed_candidate(
    tmp_path,
    monkeypatch,
) -> None:
    slot = {
        **_slots()[0],
        "planned_duration_sec": 4.0,
        "source_segment_ids": ["segment_0002"],
        "required_visible_subjects": ["focal subject"],
    }
    video_description = _video_description()
    segment = video_description["segments"][1]
    segment["time_range"]["end_sec"] = 30.0
    segment["shots"] = [
        {
            "shot_id": "shot_00002_a",
            "time_range": {"start_sec": 10.0, "end_sec": 14.0},
        },
        {
            "shot_id": "shot_00002_b",
            "time_range": {"start_sec": 14.0, "end_sec": 18.0},
        },
        {
            "shot_id": "shot_00002_c",
            "time_range": {"start_sec": 18.0, "end_sec": 30.0},
        },
    ]
    video_description["segments"][2]["time_range"] = {
        "start_sec": 30.0,
        "end_sec": 40.0,
    }
    video_description["segments"][2]["shots"][0]["time_range"] = {
        "start_sec": 30.0,
        "end_sec": 40.0,
    }
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "test"})
    context.set_artifact("video_description", video_description)
    calls: list[dict[str, object]] = []
    log_events: list[dict[str, object]] = []

    def call_prompt(**kwargs):
        package = kwargs["package"]
        segment_match = re.search(
            r"<available_source_segments_by_slot>\n(.*?)\n"
            r"</available_source_segments_by_slot>",
            package.user_prompt,
            re.DOTALL,
        )
        confirmed_match = re.search(
            r"<confirmed_candidates>\n(.*?)\n</confirmed_candidates>",
            package.user_prompt,
            re.DOTALL,
        )
        assert segment_match is not None
        assert confirmed_match is not None
        supplied_segments = json.loads(segment_match.group(1))["slot_01"]
        confirmed = json.loads(confirmed_match.group(1))["slot_01"]
        calls.append(
            {
                "operation": package.operation,
                "segment_ids": [
                    item["segment_id"] for item in supplied_segments
                ],
                "confirmed": confirmed,
            }
        )
        timestamps = (
            [
                "00:00:10,000-00:00:14,000",
                "00:00:14,000-00:00:18,000",
            ]
            if len(calls) == 1
            else ["00:00:18,000-00:00:22,000"]
        )
        response = {
            "candidates": [
                {
                    "slot_id": "slot_01",
                    "items": [
                        {
                            "timestamp": timestamp,
                            "description": "visible source content",
                            "matched_dialogue": "",
                            "semantic_relevance": 0.8,
                            "emotional_intensity": 0.5,
                            "salience": 0.7,
                        }
                        for timestamp in timestamps
                    ],
                }
            ]
        }
        return kwargs["validate_business"](response)

    def add_visual_features(_video_path, slots, pool, *_args, **_kwargs):
        for candidate in pool[slots[0]["slot_id"]]:
            accepted = candidate["timestamp"] != "00:00:14,000-00:00:18,000"
            candidate.update(
                {
                    "description": "visible source content",
                    "visible_subjects": ["focal subject"] if accepted else [],
                    "protagonist_visibility_likert": 5 if accepted else 1,
                    "visual_slot_relevance_likert": 5,
                    "visual_evidence": (
                        "clear identity" if accepted else "different identity"
                    ),
                }
            )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.log_event",
        lambda level, component, event, message, **fields: log_events.append(
            {
                "level": level,
                "component": component,
                "event": event,
                "message": message,
                **fields,
            }
        ),
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        add_visual_features,
    )

    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda _video_path, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )

    pool = retrieve_candidates(
        [slot],
        tmp_path / "video.mp4",
        LLMConfig(model="text", base_url="", api_key="test"),
        VLMConfig(model="vision", base_url="", api_key="test"),
        CandidateRetrievalConfig(
            candidates_per_slot=2,
            retrieval_max_rounds=2,
        ),
        context,
    )

    assert [call["segment_ids"] for call in calls] == [
        ["segment_0002"],
        ["segment_0002"],
    ]
    assert calls[0]["confirmed"] == []
    assert calls[1]["confirmed"] == [
        {
            "candidate_id": "slot_01_round_01_candidate_01",
            "timestamp": "00:00:10,000-00:00:14,000",
            "visible_description": "visible source content",
            "visual_evidence": "clear identity",
        }
    ]
    assert [candidate["timestamp"] for candidate in pool["slot_01"]] == [
        "00:00:10,000-00:00:14,000",
        "00:00:18,000-00:00:22,000",
    ]
    rejection_logs = [
        event
        for event in log_events
        if event["message"] == "Candidate rejected after visual diagnostics"
    ]
    assert len(rejection_logs) == 1
    rejection = rejection_logs[0]
    assert rejection["reason_code"] == "required_subject_not_visually_confirmed"
    assert rejection["diagnosis"]
    assert rejection["repair_requirement"]
    assert rejection["slot_id"] == "slot_01"
    assert rejection["candidate_id"] == "slot_01_round_01_candidate_02"
    assert rejection["required_visible_subjects"] == ["focal subject"]
    assert rejection["visible_subjects"] == []
    assert rejection["protagonist_visibility_likert"] == 1


def test_all_vlm_rejected_slots_are_replanned_in_one_batch(
    tmp_path,
    monkeypatch,
) -> None:
    slots = [
        {
            **_slots()[index - 1],
            "narrative_role": "development",
            "target_emotion": "focused",
            "planned_duration_sec": 4.0,
            "continuity_from_previous": "continues",
            "source_segment_ids": [f"segment_{index:04d}"],
            "required_visible_subjects": ["focal subject"],
        }
        for index in range(1, 3)
    ]
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "show the focal subject"})
    context.set_artifact("video_description", _video_description())
    replan_calls: list[list[dict[str, object]]] = []
    model_calls: list[str] = []
    call_lock = threading.Lock()

    def call_prompt(**kwargs):
        operation = kwargs["package"].operation
        slot_id = operation.rsplit(" ", 1)[-1]
        with call_lock:
            model_calls.append(operation)
        slot = next(item for item in slots if item["slot_id"] == slot_id)
        segment_number = int(slot["source_segment_ids"][0].rsplit("_", 1)[-1])
        start_sec = float((segment_number - 1) * 10)
        response = {
            "candidates": [
                {
                    "slot_id": slot_id,
                    "items": [
                        {
                            "timestamp": (
                                f"00:00:{int(start_sec):02d},000-"
                                f"00:00:{int(start_sec + 4):02d},000"
                            ),
                            "description": "candidate event",
                            "matched_dialogue": "",
                            "semantic_relevance": 0.8,
                            "emotional_intensity": 0.5,
                            "salience": 0.7,
                        }
                    ],
                }
            ]
        }
        return kwargs["validate_business"](response)

    def add_visual_features(_video_path, visual_slots, visual_pool, *_args, **_kwargs):
        slot_id = visual_slots[0]["slot_id"]
        for candidate in visual_pool[slot_id]:
            # Exhaust the ordinary retrieval budget first; accept only the
            # validation pass for the redesigned Slot contract.  The exact
            # round number is intentionally not part of the behaviour.
            accepted = str(
                visual_slots[0]["content_description"]
            ).startswith("supported ")
            subject_visible_but_irrelevant = not accepted and slot_id == "slot_01"
            candidate.update(
                {
                    "description": (
                        "the focal subject is visible"
                        if accepted or subject_visible_but_irrelevant
                        else "a different person is visible"
                    ),
                    "visible_subjects": (
                        ["focal subject"]
                        if accepted or subject_visible_but_irrelevant
                        else ["other"]
                    ),
                    "protagonist_visibility_likert": (
                        5 if accepted or subject_visible_but_irrelevant else 1
                    ),
                    "visual_slot_relevance_likert": 5 if accepted else 2,
                    "visual_evidence": (
                        "clear matching identity"
                        if accepted or subject_visible_but_irrelevant
                        else "clear different identity"
                    ),
                }
            )

    def replan_slots(current_slots, failures):
        replan_calls.append(failures)
        assert current_slots is slots
        assert {failure["slot_id"] for failure in failures} == {
            "slot_01",
            "slot_02",
        }
        assert all(
            failure["reason_code"] == "no_candidate_passed_visual_diagnostics"
            for failure in failures
        )
        assert all(failure["candidate_rejections"] for failure in failures)
        rejection_reasons = {
            failure["slot_id"]: failure["candidate_rejections"][0]["reason_code"]
            for failure in failures
        }
        assert rejection_reasons == {
            "slot_01": "visual_slot_not_relevant",
            "slot_02": "required_subject_not_visually_confirmed",
        }
        redesigned = [
            {
                **slot,
                "content_description": f"supported {slot['slot_id']}",
                "source_segment_ids": [f"segment_{index + 2:04d}"],
            }
            for index, slot in enumerate(current_slots)
        ]
        return redesigned, {"slot_01", "slot_02"}

    def add_kinetic_features(_video_path, pool, *_args):
        for candidates in pool.values():
            for candidate in candidates:
                candidate["kinetic_energy"] = 0.5

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        add_visual_features,
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        add_kinetic_features,
    )

    pool = retrieve_candidates(
        slots,
        tmp_path / "video.mp4",
        LLMConfig(model="text", base_url="", api_key="test", max_concurrency=2),
        VLMConfig(model="vision", base_url="", api_key="test", max_concurrency=2),
        CandidateRetrievalConfig(
            candidates_per_slot=1,
            retrieval_max_rounds=1,
            static_kinetic_energy_threshold=0.05,
        ),
        context,
        replan_slots=replan_slots,
    )

    assert len(replan_calls) == 1
    assert len(model_calls) == 8
    assert [slot["source_segment_ids"] for slot in slots] == [
        ["segment_0002"],
        ["segment_0003"],
    ]
    assert all(len(candidates) == 1 for candidates in pool.values())
    assert context.get_artifact("candidate_rejections") == []


def test_too_short_source_segment_triggers_targeted_replan_before_llm(
    tmp_path,
    monkeypatch,
) -> None:
    slot = {
        **_slots()[0],
        "narrative_role": "development",
        "target_emotion": "focused",
        "desired_duration_sec": 6.0,
        "planned_duration_sec": 6.0,
        "continuity_from_previous": "continues",
        "source_segment_ids": ["segment_0001"],
        "required_visible_subjects": [],
    }
    slots = [slot]
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "show a supported event"})
    video_description = _video_description()
    video_description["segments"][0]["time_range"]["end_sec"] = 5.0
    video_description["segments"][0]["shots"][0]["time_range"]["end_sec"] = 5.0
    context.set_artifact("video_description", video_description)
    replan_calls: list[list[dict[str, object]]] = []
    model_operations: list[str] = []

    def replan_slots(current_slots, failures):
        replan_calls.append(failures)
        assert len(failures) == 1
        expected_failure = build_prompt_failure(
            PromptFailureCode.SOURCE_SEGMENTS_TOO_SHORT,
            slot_id="slot_01",
            round=1,
            scope="planned_segments",
            scope_round=1,
            planned_duration_sec=6.0,
            longest_segment_duration_sec=5.0,
            source_segment_ids=["segment_0001"],
        )
        assert {
            key: failures[0][key]
            for key in expected_failure
        } == expected_failure
        assert failures[0]["failed_source_segment_ids"] == ["segment_0001"]
        redesigned = [
            {
                **current_slots[0],
                "source_segment_ids": ["segment_0002", "segment_0003"],
            }
        ]
        return redesigned, {"slot_01"}

    def call_prompt(**kwargs):
        model_operations.append(kwargs["package"].operation)
        response = {
            "candidates": [
                {
                    "slot_id": "slot_01",
                    "items": [
                        {
                            "timestamp": timestamp,
                            "description": "supported event",
                            "matched_dialogue": "",
                            "semantic_relevance": 0.8,
                            "emotional_intensity": 0.5,
                            "salience": 0.7,
                        }
                        for timestamp in (
                            "00:00:10,000-00:00:16,000",
                            "00:00:20,000-00:00:26,000",
                        )
                    ],
                }
            ]
        }
        return kwargs["validate_business"](response)

    def add_visual_features(_video_path, visual_slots, visual_pool, *_args, **_kwargs):
        for candidate in visual_pool[visual_slots[0]["slot_id"]]:
            candidate.update(
                {
                    "description": "supported event",
                    "visible_subjects": [],
                    "protagonist_visibility_likert": 2,
                    "visual_slot_relevance_likert": 5,
                    "visual_evidence": "the event is visible",
                }
            )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        add_visual_features,
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda _video_path, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )

    pool = retrieve_candidates(
        slots,
        tmp_path / "video.mp4",
        LLMConfig(model="text", base_url="", api_key="test"),
        VLMConfig(model="vision", base_url="", api_key="test"),
        CandidateRetrievalConfig(
            candidates_per_slot=2,
            retrieval_max_rounds=1,
        ),
        context,
        replan_slots=replan_slots,
    )

    assert len(replan_calls) == 1
    assert model_operations == ["Candidate retrieval round 2 slot slot_01"]
    assert len(pool["slot_01"]) == 2


def test_adjacent_segment_round_overretrieves_threefold_and_keeps_best_scores(
    tmp_path,
    monkeypatch,
) -> None:
    slot = {
        **_slots()[0],
        "planned_duration_sec": 4.0,
        "source_segment_ids": ["segment_0002"],
        "required_visible_subjects": [],
    }
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "test"})
    context.set_artifact("video_description", _video_description())
    operations: list[str] = []

    def call_prompt(**kwargs):
        package = kwargs["package"]
        operations.append(package.operation)
        if len(operations) <= 4:
            raise RuntimeError("force adjacent expansion")
        timestamps = [
            "00:00:00,000-00:00:04,000",
            "00:00:01,000-00:00:05,000",
            "00:00:02,000-00:00:06,000",
            "00:00:10,000-00:00:14,000",
            "00:00:11,000-00:00:15,000",
            "00:00:12,000-00:00:16,000",
            "00:00:20,000-00:00:24,000",
            "00:00:21,000-00:00:25,000",
            "00:00:22,000-00:00:26,000",
        ]
        response = {
            "candidates": [
                {
                    "slot_id": "slot_01",
                    "items": [
                        {
                            "timestamp": timestamp,
                            "description": "visible source content",
                            "matched_dialogue": "",
                            "semantic_relevance": (index + 1) / 10,
                            "emotional_intensity": 0.5,
                            "salience": 0.7,
                        }
                        for index, timestamp in enumerate(timestamps)
                    ],
                }
            ]
        }
        return kwargs["validate_business"](response)

    def add_visual_features(_video_path, slots, pool, *_args, **_kwargs):
        for candidate in pool[slots[0]["slot_id"]]:
            candidate.update(
                {
                    "description": "visible source content",
                    "visible_subjects": [],
                    "protagonist_visibility_likert": 2,
                    "visual_slot_relevance_likert": 5,
                    "visual_evidence": "clear visible event",
                }
            )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        add_visual_features,
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda _video_path, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )

    pool = retrieve_candidates(
        [slot],
        tmp_path / "video.mp4",
        LLMConfig(model="text", base_url="", api_key="test"),
        VLMConfig(model="vision", base_url="", api_key="test"),
        CandidateRetrievalConfig(
            candidates_per_slot=3,
            retrieval_max_rounds=3,
        ),
        context,
    )

    assert operations == [
        f"Candidate retrieval round {round_index} slot slot_01"
        for round_index in range(1, 6)
    ]
    assert [candidate["timestamp"] for candidate in pool["slot_01"]] == [
        "00:00:02,000-00:00:06,000",
        "00:00:12,000-00:00:16,000",
        "00:00:22,000-00:00:26,000",
    ]


def test_underfilled_nonempty_candidate_pool_continues_after_all_scopes(
    tmp_path,
    monkeypatch,
) -> None:
    slot = {
        **_slots()[0],
        "planned_duration_sec": 4.0,
        "source_segment_ids": ["segment_0002"],
        "required_visible_subjects": ["focal subject"],
    }
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "test"})
    context.set_artifact("video_description", _video_description())
    retrieval_call = 0

    def call_prompt(**kwargs):
        nonlocal retrieval_call
        retrieval_call += 1
        timestamps = {
            1: [
                "00:00:10,000-00:00:14,000",
                "00:00:11,000-00:00:15,000",
                "00:00:12,000-00:00:16,000",
            ],
            2: [
                "00:00:13,000-00:00:17,000",
                "00:00:14,000-00:00:18,000",
            ],
            3: [
                "00:00:00,000-00:00:04,000",
                "00:00:01,000-00:00:05,000",
                "00:00:02,000-00:00:06,000",
                "00:00:20,000-00:00:24,000",
                "00:00:21,000-00:00:25,000",
                "00:00:22,000-00:00:26,000",
            ],
        }[retrieval_call]
        response = {
            "candidates": [
                {
                    "slot_id": "slot_01",
                    "items": [
                        {
                            "timestamp": timestamp,
                            "description": "visible source content",
                            "matched_dialogue": "",
                            "semantic_relevance": 0.8,
                            "emotional_intensity": 0.5,
                            "salience": 0.7,
                        }
                        for timestamp in timestamps
                    ],
                }
            ]
        }
        return kwargs["validate_business"](response)

    def add_visual_features(_video_path, slots, pool, *_args, **_kwargs):
        for candidate in pool[slots[0]["slot_id"]]:
            accepted = candidate["timestamp"] == "00:00:10,000-00:00:14,000"
            candidate.update(
                {
                    "description": "visible source content",
                    "visible_subjects": ["focal subject"] if accepted else [],
                    "protagonist_visibility_likert": 5 if accepted else 1,
                    "visual_slot_relevance_likert": 5,
                    "visual_evidence": (
                        "clear identity" if accepted else "different identity"
                    ),
                }
            )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        add_visual_features,
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda _video_path, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )

    pool = retrieve_candidates(
        [slot],
        tmp_path / "video.mp4",
        LLMConfig(model="text", base_url="", api_key="test"),
        VLMConfig(model="vision", base_url="", api_key="test"),
        CandidateRetrievalConfig(
            candidates_per_slot=3,
            retrieval_max_rounds=1,
        ),
        context,
    )

    assert len(pool["slot_01"]) == 1
    assert context.get_artifact("candidate_shortages") == {"slot_01": 2}
    assert context.get_artifact("retrieval_failure") is None


def test_candidate_retrieval_runs_one_slot_per_concurrent_model_request(
    tmp_path,
    monkeypatch,
) -> None:
    slots = [
        {
            **slot,
            "planned_duration_sec": 5.0,
            "source_segment_ids": [f"segment_{index:04d}"],
            "required_visible_subjects": [],
        }
        for index, slot in enumerate(
            [
                _slots()[0],
                _slots()[1],
                {
                    **_slots()[0],
                    "slot_id": "slot_03",
                    "content_description": "resolution",
                },
            ],
            1,
        )
    ]
    context = WorkflowContext(tmp_path / "history.json")
    context.set_artifact("request", {"instruction": "test"})
    context.set_artifact("video_description", _video_description())
    barrier = threading.Barrier(len(slots))
    prompt_slot_ids = []
    visual_slot_ids = []

    def call_prompt(**kwargs):
        package = kwargs["package"]
        match = re.search(r"<slots>\n(.*?)\n</slots>", package.user_prompt, re.DOTALL)
        assert match is not None
        prompt_slots = json.loads(match.group(1))
        assert len(prompt_slots) == 1
        slot = prompt_slots[0]
        slot_id = slot["slot_id"]
        prompt_slot_ids.append(slot_id)
        barrier.wait(timeout=2)
        segment_index = int(slot["source_segment_ids"][0].split("_")[-1])
        start = (segment_index - 1) * 10
        response = {
            "candidates": [
                {
                    "slot_id": slot_id,
                    "items": [
                        {
                            "timestamp": (
                                f"00:00:{start:02d},000-00:00:{start + 5:02d},000"
                            ),
                            "description": "visible source content",
                            "matched_dialogue": "",
                            "semantic_relevance": 0.8,
                            "emotional_intensity": 0.5,
                            "salience": 0.7,
                        }
                    ],
                }
            ]
        }
        return kwargs["validate_business"](response)

    def add_visual_features(_video_path, visual_slots, visual_pool, *_args, **_kwargs):
        assert len(visual_slots) == 1
        slot_id = visual_slots[0]["slot_id"]
        visual_slot_ids.append(slot_id)
        for candidate in visual_pool[slot_id]:
            candidate.update(
                {
                    "description": "visible source content",
                    "visible_subjects": [],
                    "protagonist_visibility_likert": 2,
                    "visual_slot_relevance_likert": 4,
                    "visual_evidence": "sampled frames match",
                }
            )

    monkeypatch.setattr(context, "call_prompt", call_prompt)
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_visual_features",
        add_visual_features,
    )
    monkeypatch.setattr(
        "cutmaster.workflow.planners.timeline_scout.add_kinetic_features",
        lambda _video_path, pool, *_args: [
            candidate.update({"kinetic_energy": 0.5})
            for candidates in pool.values()
            for candidate in candidates
        ],
    )

    pool = retrieve_candidates(
        slots,
        tmp_path / "video.mp4",
        LLMConfig(
            model="text",
            base_url="",
            api_key="test",
            max_concurrency=3,
        ),
        VLMConfig(
            model="vision",
            base_url="",
            api_key="test",
            max_concurrency=2,
        ),
        CandidateRetrievalConfig(
            candidates_per_slot=1,
            retrieval_max_rounds=1,
        ),
        context,
    )

    assert set(prompt_slot_ids) == {"slot_01", "slot_02", "slot_03"}
    assert set(visual_slot_ids) == {"slot_01", "slot_02", "slot_03"}
    assert all(len(candidates) == 1 for candidates in pool.values())


def test_visual_likert_scores_are_normalized_for_unary() -> None:
    slot = _slots()[0] | {
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


def test_review_accepts_maximal_feasible_patch_subset(tmp_path, monkeypatch) -> None:
    slots = [
        {
            "slot_id": f"slot_{index:02d}",
            "content_description": f"scene {index}",
            "target_emotional_intensity": 0.5,
            "target_kinetic_energy": 0.5,
            "planned_duration_sec": 5.0,
            "output_start_sec": float((index - 1) * 5),
            "output_end_sec": float(index * 5),
        }
        for index in range(1, 4)
    ]

    def candidate(slot_id, candidate_id, timestamp):
        return {
            "slot_id": slot_id,
            "candidate_id": candidate_id,
            "timestamp": timestamp,
            "description": "visible scene",
            "semantic_relevance": 0.9,
            "visual_slot_relevance_likert": 5,
            "protagonist_visibility_likert": 5,
            "emotional_intensity": 0.5,
            "kinetic_energy": 0.5,
            "salience": 0.9,
        }

    pool = {
        "slot_01": [
            candidate("slot_01", "slot_01_candidate_01", "00:00:00,000-00:00:05,000"),
            candidate("slot_01", "slot_01_candidate_02", "00:00:08,000-00:00:13,000"),
        ],
        "slot_02": [
            candidate("slot_02", "slot_02_candidate_01", "00:00:10,000-00:00:15,000"),
        ],
        "slot_03": [
            candidate("slot_03", "slot_03_candidate_01", "00:00:20,000-00:00:25,000"),
            candidate("slot_03", "slot_03_candidate_02", "00:00:30,000-00:00:35,000"),
        ],
    }
    original = path_to_script(
        slots,
        [pool["slot_01"][0], pool["slot_02"][0], pool["slot_03"][0]],
        __import__("pathlib").Path("video.mp4"),
    )
    context = WorkflowContext(tmp_path / "history.json")
    proposed = [
        {
            "operation": "replace",
            "slot_id": "slot_01",
            "candidate_id": "slot_01_candidate_02",
            "reason": "conflicts with slot 2",
        },
        {
            "operation": "replace",
            "slot_id": "slot_03",
            "candidate_id": "slot_03_candidate_02",
            "reason": "safe improvement",
        },
    ]
    monkeypatch.setattr(context, "call_prompt", lambda **_kwargs: proposed)
    pairwise_scores = {
        _pair_key("slot_01_candidate_01", "slot_02_candidate_01"): {
            "pairwise_score": 0.5
        },
        _pair_key("slot_02_candidate_01", "slot_03_candidate_01"): {
            "pairwise_score": 0.5
        },
        _pair_key("slot_02_candidate_01", "slot_03_candidate_02"): {
            "pairwise_score": 0.9
        },
    }
    patched, accepted = review_and_patch(
        slots,
        pool,
        original,
        __import__("cutmaster.configuration.schema", fromlist=["LLMConfig"]).LLMConfig(
            model="test", base_url="", api_key="test"
        ),
        context,
        pairwise_scores,
    )
    assert [patch["slot_id"] for patch in accepted] == ["slot_03"]
    assert patched[2]["candidate_id"] == "slot_03_candidate_02"
    assert context.data["script_versions"][-1]["rejected_patches"][0]["slot_id"] == "slot_01"


def test_review_rejects_patch_that_degrades_lazy_hard_cut(
    tmp_path,
    monkeypatch,
) -> None:
    slots = [
        {
            "slot_id": f"slot_{index:02d}",
            "content_description": f"scene {index}",
            "target_emotional_intensity": 0.5,
            "target_kinetic_energy": 0.5,
            "planned_duration_sec": 4.0,
            "output_start_sec": float((index - 1) * 4),
            "output_end_sec": float(index * 4),
        }
        for index in range(1, 3)
    ]

    def candidate(slot_id, candidate_id, timestamp):
        return {
            "slot_id": slot_id,
            "candidate_id": candidate_id,
            "timestamp": timestamp,
            "description": "focal subject visible",
            "semantic_relevance": 1.0,
            "visual_slot_relevance_likert": 5,
            "protagonist_visibility_likert": 5,
            "emotional_intensity": 0.5,
            "kinetic_energy": 0.5,
            "salience": 1.0,
        }

    first = candidate("slot_01", "a", "00:00:01,000-00:00:05,000")
    original_second = candidate("slot_02", "b", "00:00:06,000-00:00:10,000")
    replacement_second = candidate("slot_02", "c", "00:00:11,000-00:00:15,000")
    pool = {
        "slot_01": [first],
        "slot_02": [original_second, replacement_second],
    }
    original = path_to_script(
        slots,
        [first, original_second],
        __import__("pathlib").Path("video.mp4"),
    )
    context = WorkflowContext(tmp_path / "history.json")
    monkeypatch.setattr(
        context,
        "call_prompt",
        lambda **_kwargs: [
            {
                "operation": "replace",
                "slot_id": "slot_02",
                "candidate_id": "c",
                "reason": "text-only preference",
            }
        ],
    )
    patched, accepted = review_and_patch(
        slots,
        pool,
        original,
        LLMConfig(model="test", base_url="", api_key="test"),
        context,
        {
            _pair_key("a", "b"): {"pairwise_score": 1.0},
            _pair_key("a", "c"): {"pairwise_score": 0.0},
        },
    )
    assert accepted == []
    assert patched[1]["candidate_id"] == "b"
    rejected = context.data["script_versions"][-1]["rejected_patches"]
    assert rejected[0]["rejection"]["reason_code"] == (
        "patch_degrades_or_requires_unscored_path"
    )
