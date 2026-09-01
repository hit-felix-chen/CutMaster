from __future__ import annotations

import json

import pytest

from cutmaster.workflow.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.workflow.prompting.analyser import (
    SegmentSummaryDetails,
    SegmentShotAnnotationDetails,
    VideoSummaryDetails,
)
from cutmaster.workflow.prompting.core import response_template_from_schema
from cutmaster.workflow.prompting.failure_catalog import (
    PROMPT_FAILURE_CATALOG,
    PromptFailureCode,
    build_prompt_failure,
)
from cutmaster.workflow.prompting.planners import (
    CandidateRetrievalDetails,
    CandidateVisualScoringDetails,
    DialogueAnchorSelectionDetails,
    ScriptReviewDetails,
    SlotArrangementDetails,
)


def _shot_package():
    return prompt_registry.build(
        PromptStage.ANALYSER,
        PromptTask.SHOT_ANNOTATION,
        SegmentShotAnnotationDetails(
            segment={
                "segment_id": "segment_0001",
                "has_dialogue": False,
                "speech_mode": "none",
                "shots": [
                    {
                        "shot_id": "shot_00001",
                        "timestamp": "00:00:00,000-00:00:01,000",
                        "dialogue": [],
                    }
                ],
            },
            sampled_frame_times_by_shot={
                "shot_00001": [0.1, 0.3, 0.5, 0.7, 0.9]
            },
        ),
    )


def test_registry_exposes_every_model_task() -> None:
    assert set(prompt_registry.registered_keys()) == {
        (PromptStage.ANALYSER, PromptTask.DIALOGUE_RECONSTRUCTION),
        (PromptStage.ANALYSER, PromptTask.SCENE_BOUNDARY_DETECTION),
        (PromptStage.ANALYSER, PromptTask.SHOT_ANNOTATION),
        (PromptStage.ANALYSER, PromptTask.SEGMENT_SUMMARY),
        (PromptStage.ANALYSER, PromptTask.VIDEO_SUMMARY),
        (PromptStage.PLANNERS, PromptTask.SLOT_ARRANGEMENT),
        (PromptStage.PLANNERS, PromptTask.DIALOGUE_ANCHOR_SELECTION),
        (PromptStage.PLANNERS, PromptTask.CANDIDATE_RETRIEVAL),
        (PromptStage.PLANNERS, PromptTask.CANDIDATE_VISUAL_SCORING),
        (PromptStage.PLANNERS, PromptTask.PAIRWISE_SCORING),
        (PromptStage.PLANNERS, PromptTask.SCRIPT_REVIEW),
    }


def test_candidate_visual_scoring_is_scoped_only_to_slot_requirements() -> None:
    package = prompt_registry.build(
        PromptStage.PLANNERS,
        PromptTask.CANDIDATE_VISUAL_SCORING,
        CandidateVisualScoringDetails(
            operation="Candidate visual validation",
            candidates=[
                {
                    "candidate_id": "slot_01_candidate_01",
                    "slot_id": "slot_01",
                    "intended_visible_content": "The coach directs the defensive line.",
                    "required_visible_subjects": ["Coach", "Defenders"],
                    "source_segment_video_descriptions": [],
                }
            ],
        ),
    )

    assert package.context_keys == ()
    assert package.prompt_version == "2.1"
    assert "maintained request" not in package.system_prompt.lower()
    assert "global user request" in package.user_prompt
    assert "do not infer or enforce" in package.user_prompt.lower()
    assert "all subjects in required_visible_subjects" in package.user_prompt


def test_prompt_embeds_contract_and_template_derived_from_same_schema() -> None:
    package = _shot_package()
    schema = package.response_contract.schema
    template = response_template_from_schema(schema)

    assert json.dumps(schema, ensure_ascii=False, indent=2) in package.user_prompt
    assert json.dumps(template, ensure_ascii=False, indent=2) in package.user_prompt
    assert len(package.response_contract.fingerprint) == 16
    assert len(package.fingerprint) == 16


def test_segment_summary_prompt_requires_one_concise_summary() -> None:
    package = prompt_registry.build(
        PromptStage.ANALYSER,
        PromptTask.SEGMENT_SUMMARY,
        SegmentSummaryDetails(
            segment={
                "segment_id": "segment_0001",
                "time_range": {"start_sec": 0.0, "end_sec": 4.0},
                "dialogue_items": [],
                "shots": [
                    {
                        "shot_id": "shot_00001",
                        "visual_description": "A person enters the room.",
                    }
                ],
            }
        ),
    )
    summary_schema = package.response_contract.schema["properties"][
        "segment_summary"
    ]

    assert package.response_contract.version == "2.0"
    assert summary_schema["maxLength"] == 600
    assert "one to three sentences" in package.user_prompt
    assert "every label may appear multiple times or not appear at all" in package.user_prompt
    assert package.context_keys == ()


def test_slot_arrangement_contract_leaves_slot_count_to_model() -> None:
    package = prompt_registry.build(
        PromptStage.PLANNERS,
        PromptTask.SLOT_ARRANGEMENT,
        SlotArrangementDetails(
            target_duration_sec=60.0,
            target_clip_duration_sec=4.0,
            allowed_segment_ids=["segment_0001", "segment_0002"],
            retry_note="",
            mode="full",
            existing_slots=[],
            target_slot_constraints={},
            rejection_feedback=[],
        ),
    )
    slots_schema = package.response_contract.schema["properties"]["slots"]

    assert slots_schema["minItems"] == 1
    assert "maxItems" not in slots_schema
    assert "no predetermined clip count" in package.user_prompt
    assert "target_clip_duration_sec=4.0" in package.user_prompt
    assert "individual Slot may exceed\n6.0 seconds" in package.user_prompt
    assert "Dialogue length never justifies making a visual Slot longer" in package.user_prompt
    assert "as a\nstart-aligned L-cut" in package.user_prompt
    assert "Every role may" in package.user_prompt
    assert "appear multiple times or not appear at all" in package.user_prompt
    assert "do not create one Slot per enum value" in package.user_prompt
    assert "totaling the requested output" in package.user_prompt
    assert "duration within 0.5 seconds" in package.user_prompt
    assert "opening or end" in package.user_prompt
    assert "credits, production logos" in package.user_prompt
    assert "Source quality, request relevance, and narrative value always take priority" in (
        package.user_prompt
    )
    slot_item_schema = slots_schema["items"]
    assert "source_segment_id" in slot_item_schema["required"]
    assert "source_segment_ids" not in slot_item_schema["properties"]
    assert "Adjacent Slots may share one Segment" in package.user_prompt
    assert "their total desired duration must fit inside that Segment" in package.user_prompt
    assert "monotonically non-decreasing" in package.user_prompt
    assert "Slot Group Segment\nidentifiers are strictly increasing" in package.user_prompt
    assert "distribute source_segment_id values as evenly as practical" in package.user_prompt
    assert "reserve sufficient chronological Segment space" in package.user_prompt
    assert package.prompt_version == "4.0"
    assert package.context_keys == (
        "request",
        "music_profile",
        "source_story_context",
        "planners_feedback",
    )


def test_targeted_slot_arrangement_contract_batches_exact_requested_slots() -> None:
    constraints = {
        "slot_02": {
            "desired_duration_sec": 3.0,
            "planned_duration_ms": 3200,
            "allowed_segment_ids": ["segment_0001", "segment_0002"],
            "original_group_id": "group_001",
        },
        "slot_04": {
            "desired_duration_sec": 4.0,
            "planned_duration_ms": 3800,
            "allowed_segment_ids": ["segment_0003", "segment_0004"],
            "original_group_id": "group_002",
        },
    }
    package = prompt_registry.build(
        PromptStage.PLANNERS,
        PromptTask.SLOT_ARRANGEMENT,
        SlotArrangementDetails(
            target_duration_sec=20.0,
            target_clip_duration_sec=4.0,
            allowed_segment_ids=[],
            retry_note="",
            mode="targeted",
            existing_slots=[{"slot_id": "slot_01"}],
            target_slot_constraints=constraints,
            rejection_feedback=[
                build_prompt_failure(
                    PromptFailureCode.NO_CANDIDATE_PASSED_VISUAL_DIAGNOSTICS,
                    slot_id="slot_02",
                    candidate_rejections=[],
                ),
                build_prompt_failure(
                    PromptFailureCode.SOURCE_SEGMENTS_TOO_SHORT,
                    slot_id="slot_04",
                    longest_segment_duration_sec=3.0,
                    planned_duration_sec=4.0,
                ),
            ],
        ),
    )
    slots_schema = package.response_contract.schema["properties"]["slots"]

    assert package.operation == "Arrangement Architect targeted repair"
    assert slots_schema["minItems"] == 2
    assert slots_schema["maxItems"] == 2
    assert {
        schema["properties"]["slot_id"]["const"]
        for schema in slots_schema["items"]["oneOf"]
    } == {"slot_02", "slot_04"}
    planned_durations = {
        schema["properties"]["slot_id"]["const"]: schema["properties"][
            "planned_duration_ms"
        ]["const"]
        for schema in slots_schema["items"]["oneOf"]
    }
    assert planned_durations == {"slot_02": 3200, "slot_04": 3800}
    assert all(
        "planned_duration_ms" in schema["required"]
        for schema in slots_schema["items"]["oneOf"]
    )
    assert all(
        "source_segment_id" in schema["required"]
        and "source_segment_ids" not in schema["properties"]
        for schema in slots_schema["items"]["oneOf"]
    )
    assert "single response" in package.user_prompt
    assert "planned_duration_ms values are authoritative" in package.user_prompt
    assert "complete adjacent groups" in package.user_prompt
    assert "Every member of that\ngroup must choose the same source_segment_id" in (
        package.user_prompt
    )
    assert "combined duration must not exceed the Segment's complete source duration" in (
        package.user_prompt
    )
    assert "capacity check is for the whole resulting group" in package.user_prompt
    assert "Source quality and relevance to the maintained request always take priority" in (
        package.user_prompt
    )
    assert "runs Story Editor again" in package.user_prompt
    assert "distribute source_segment_id" not in package.user_prompt
    assert "one complete planned clip" not in package.user_prompt
    assert package.prompt_version == "4.1"
    assert "<existing_slot_plan>" in package.user_prompt
    assert "<rejection_feedback>" in package.user_prompt
    assert "planners_feedback" not in package.context_keys


def test_dialogue_anchor_contract_selects_a_contiguous_range() -> None:
    package = prompt_registry.build(
        PromptStage.PLANNERS,
        PromptTask.DIALOGUE_ANCHOR_SELECTION,
        DialogueAnchorSelectionDetails(
            slots=[
                {
                    "slot_id": "slot_01",
                    "group_id": "group_001",
                    "planned_duration_ms": 4000,
                    "planned_duration_sec": 4.0,
                    "source_segment_id": "segment_0001",
                }
            ],
            video_summary={
                "schema_version": "1.0",
                "title": "A Test Story",
                "logline": "Mia confronts a consequential truth.",
                "synopsis": "Mia seeks an answer that changes her decision.",
                "chronological_story_beats": [],
                "character_arcs": [],
                "themes": ["truth"],
                "ending": "Mia understands what she must do.",
            },
            source_segments=[
                {
                    "segment_id": "segment_0001",
                    "time_range": {"start_sec": 10.0, "end_sec": 20.0},
                    "segment_summary": "Mia presses Sebastian for the truth.",
                    "narrative_function": "Reveals the central conflict.",
                    "emotional_tone": "tense",
                    "emotional_intensity": 0.8,
                    "appearing_characters": ["Mia", "Sebastian"],
                    "dialogue_items": [
                        {
                            "dialogue_id": "7",
                            "speaker": "Mia",
                            "text": "Is that true?",
                            "time_range": {
                                "start_sec": 11.0,
                                "end_sec": 11.5,
                            },
                            "duration_sec": 0.5,
                            "source_shot_ids": ["shot_0007"],
                        },
                        {
                            "dialogue_id": "8",
                            "speaker": "Sebastian",
                            "text": "It is.",
                            "time_range": {
                                "start_sec": 11.8,
                                "end_sec": 12.4,
                            },
                            "duration_sec": 0.6,
                            "source_shot_ids": ["shot_0008"],
                        },
                    ],
                }
            ],
            dialogue_constraints_by_slot={
                "slot_01": {
                    "allowed_segment_ids": ["segment_0001"],
                    "allowed_last_dialogue_ids_by_segment_and_first": {
                        "segment_0001": {"7": ["8"]}
                    },
                    "output_audio_end_sec_by_segment_and_first_and_last": {
                        "segment_0001": {"7": {"8": 1.4}}
                    },
                    "min_audio_duration_sec": 1.0,
                    "max_audio_duration_sec": 60.0,
                    "planned_picture_duration_sec": 4.0,
                    "output_audio_start_sec": 0.0,
                    "has_same_segment_sibling_slots": True,
                    "preferred_anchor_picture_range": {
                        "source_segment_id": "segment_0001",
                        "start_sec": 10.0,
                        "end_sec": 20.0,
                        "left_slot_count": 0,
                        "right_slot_count": 0,
                    },
                }
            },
            max_anchors=3,
            min_anchor_duration_sec=1.0,
        ),
    )
    schema = package.response_contract.schema
    anchors = schema["properties"]["anchors"]
    anchor = anchors["items"]["oneOf"][0]

    assert package.response_contract.version == "4.3"
    assert anchors["minItems"] == 1
    with pytest.raises(ValueError, match="should be non-empty"):
        package.response_contract.validate_structure({"anchors": []})
    assert "Return at least one strong Anchor" in package.user_prompt
    assert package.prompt_version == "4.7"
    assert anchor["required"] == [
        "slot_id",
        "source_segment_id",
        "first_dialogue_id",
        "last_dialogue_id",
        "narrative_significance",
        "request_relevance",
        "standalone_meaning",
        "importance_likert",
        "coherence_likert",
    ]
    assert anchor["properties"]["source_segment_id"]["enum"] == [
        "segment_0001"
    ]
    assert anchor["properties"]["first_dialogue_id"]["enum"] == ["7"]
    assert anchor["properties"]["last_dialogue_id"]["enum"] == ["8"]
    assert "allOf" not in anchor
    assert "<source_shots>" not in package.user_prompt
    assert "inclusive endpoints" in package.user_prompt
    assert "allowed_last_dialogue_ids_by_segment_and_first" not in package.user_prompt
    assert "output_audio_end_sec_by_segment_and_first_and_last" in package.user_prompt
    assert "allowed_passages" not in package.user_prompt
    assert '"8": 1.4' in package.user_prompt
    assert "validates continuity" in package.user_prompt
    assert "same L-cut layout" in package.user_prompt
    assert "preferred_anchor_picture_range" in package.user_prompt
    assert "soft guidance, not a hard constraint" in package.user_prompt
    assert "anchor_picture_edge_imbalance_ms_by_segment_and_first" not in (
        package.user_prompt
    )
    assert "audio_cut_style" not in anchor["properties"]
    assert "<video_summary>" in package.user_prompt
    assert "<dialogue_constraints_by_slot>" in package.user_prompt
    assert "<preserved_anchors>" in package.user_prompt
    assert "<valid_dialogue_ranges_by_slot>" not in package.user_prompt
    assert "Mia presses Sebastian for the truth." in package.user_prompt
    assert "<full_dialogue_context>" not in package.user_prompt
    assert "music_profile" not in package.context_keys
    assert "planners_feedback" in package.context_keys
    assert "lip" not in package.user_prompt.lower()


def test_dialogue_anchor_contract_stays_compact_for_many_endpoint_pairs() -> None:
    first_ids = [f"first_{index:03d}" for index in range(100)]
    last_ids = [f"last_{index:03d}" for index in range(30)]
    dialogue_ids = [*first_ids, *last_ids]
    allowed_last_ids = {
        first_dialogue_id: list(last_ids)
        for first_dialogue_id in first_ids
    }
    output_end_by_endpoint = {
        first_dialogue_id: {
            last_dialogue_id: round(2.0 + index * 0.01, 6)
            for index, last_dialogue_id in enumerate(last_ids)
        }
        for first_dialogue_id in first_ids
    }
    package = prompt_registry.build(
        PromptStage.PLANNERS,
        PromptTask.DIALOGUE_ANCHOR_SELECTION,
        DialogueAnchorSelectionDetails(
            slots=[{"slot_id": "slot_01"}],
            video_summary={"story": "A compact contract test."},
            source_segments=[
                {
                    "segment_id": "segment_0001",
                    "dialogue_items": [
                        {
                            "dialogue_id": dialogue_id,
                            "speaker": "Speaker",
                            "text": dialogue_id,
                        }
                        for dialogue_id in dialogue_ids
                    ],
                }
            ],
            dialogue_constraints_by_slot={
                "slot_01": {
                    "allowed_segment_ids": ["segment_0001"],
                    "allowed_last_dialogue_ids_by_segment_and_first": {
                        "segment_0001": allowed_last_ids
                    },
                    "output_audio_end_sec_by_segment_and_first_and_last": {
                        "segment_0001": output_end_by_endpoint
                    },
                    "output_audio_start_sec": 0.0,
                }
            },
            max_anchors=1,
            min_anchor_duration_sec=1.0,
        ),
    )

    anchor_schema = package.response_contract.schema["properties"]["anchors"][
        "items"
    ]["oneOf"][0]
    assert "allOf" not in anchor_schema
    assert len(anchor_schema["properties"]["first_dialogue_id"]["enum"]) == len(
        first_ids
    )
    assert len(anchor_schema["properties"]["last_dialogue_id"]["enum"]) == len(
        last_ids
    )
    assert len(package.user_prompt) < 200_000
    assert len(json.dumps(package.response_contract.schema)) < 30_000


def test_candidate_retrieval_contract_returns_indivisible_group_trajectories() -> None:
    package = prompt_registry.build(
        PromptStage.PLANNERS,
        PromptTask.CANDIDATE_RETRIEVAL,
        CandidateRetrievalDetails(
            operation="Candidate retrieval round 1 group_001",
            round_index=1,
            round_phase="initial",
            trajectories_per_group=2,
            group={
                "group_id": "group_001",
                "planning_segment_id": "segment_0001_01",
                "slot_ids": ["slot_01", "slot_02"],
            },
            slots=[
                {"slot_id": "slot_01", "planned_duration_ms": 2000},
                {"slot_id": "slot_02", "planned_duration_ms": 3000},
            ],
            confirmed_trajectories=[],
            excluded_ranges_by_slot={"slot_01": [], "slot_02": []},
            rejected_trajectory_signatures=[],
            rejection_feedback=[],
            planning_segment={
                "planning_segment_id": "segment_0001_01",
                "start_ms": 10000,
                "end_ms": 20000,
            },
        ),
    )
    trajectories = package.response_contract.schema["properties"]["trajectories"]
    items = trajectories["items"]["properties"]["items"]

    assert package.response_contract.version == "2.0"
    assert package.prompt_version == "3.1"
    assert trajectories["minItems"] == 2
    assert trajectories["maxItems"] == 2
    assert items["minItems"] == 2
    assert items["maxItems"] == 2
    assert {
        schema["properties"]["slot_id"]["const"]
        for schema in items["items"]["oneOf"]
    } == {"slot_01", "slot_02"}
    assert "one indivisible choice" in package.user_prompt
    assert "exactly one item for every supplied Slot" in package.user_prompt
    assert "one supplied Planning Segment" in package.user_prompt
    assert "internally ordered and non-overlapping" in package.user_prompt
    assert "planned_duration_ms to millisecond precision" in package.user_prompt
    assert "initial phase" in package.user_prompt
    assert "<rejection_feedback>" in package.user_prompt


def test_script_review_contract_replaces_whole_group_trajectory() -> None:
    package = prompt_registry.build(
        PromptStage.PLANNERS,
        PromptTask.SCRIPT_REVIEW,
        ScriptReviewDetails(
            slots=[
                {"slot_id": "slot_01", "group_id": "group_001"},
                {"slot_id": "slot_02", "group_id": "group_001"},
            ],
            candidate_pool={
                "group_001": [
                    {"trajectory_id": "trajectory_001"},
                    {"trajectory_id": "trajectory_002"},
                ]
            },
        ),
    )
    patch = package.response_contract.schema["properties"]["patches"]["items"][
        "oneOf"
    ][0]

    assert patch["properties"]["group_id"]["const"] == "group_001"
    assert patch["properties"]["trajectory_id"]["enum"] == [
        "trajectory_001",
        "trajectory_002",
    ]
    assert "one complete trajectory" in package.user_prompt
    assert "Never replace one Slot" in package.user_prompt
    assert package.prompt_version == "2.0"


def test_video_summary_contract_is_grounded_in_known_segments() -> None:
    package = prompt_registry.build(
        PromptStage.ANALYSER,
        PromptTask.VIDEO_SUMMARY,
        VideoSummaryDetails(segment_ids=["segment_0001", "segment_0002"]),
    )
    schema = package.response_contract.schema
    beat = schema["properties"]["chronological_story_beats"]["items"]

    assert package.response_contract.version == "1.0"
    assert beat["properties"]["source_segment_ids"]["items"]["enum"] == [
        "segment_0001",
        "segment_0002",
    ]
    assert package.prompt_version == "2.0"
    assert package.context_keys == (
        "source_metadata",
        "video_summary_context",
    )
    assert package.output_artifact == "video_summary"
    assert "every Segment field except the internal shots list" in package.user_prompt
    assert "complete chronological ASR transcript" in package.user_prompt


def test_registry_rebuilds_prompt_with_accumulated_failure_reasons() -> None:
    package = _shot_package()
    assert package.retry_builder is not None

    retried = package.retry_builder(
        (
            "Candidate duration differs from planned_duration_ms",
            "Duplicate candidate range for slot_01",
        )
    )

    assert '"attempt": 1' in retried.user_prompt
    assert '"attempt": 2' in retried.user_prompt
    assert "Candidate duration differs from planned_duration_ms" in retried.user_prompt
    assert "Duplicate candidate range for slot_01" in retried.user_prompt
    assert retried.user_prompt.count('"reason_code": "response_validation_failed"') == 2
    assert retried.user_prompt.count('"diagnosis":') >= 2
    assert retried.user_prompt.count('"repair_requirement":') >= 2
    assert retried.user_prompt.count("<previous_attempt_failures>") == 1


def test_prompt_failure_catalog_covers_every_reason_code() -> None:
    assert set(PROMPT_FAILURE_CATALOG) == set(PromptFailureCode)
    assert all(item.diagnosis.strip() for item in PROMPT_FAILURE_CATALOG.values())
    assert all(
        item.repair_requirement.strip()
        for item in PROMPT_FAILURE_CATALOG.values()
    )


def test_group_retrieval_failures_use_trajectory_contract() -> None:
    rejected = build_prompt_failure(
        PromptFailureCode.NO_CANDIDATE_PASSED_VISUAL_DIAGNOSTICS,
        group_id="group_001",
        candidate_rejections=[],
    )
    exhausted = build_prompt_failure(
        PromptFailureCode.INSUFFICIENT_VISUALLY_GROUNDED_CANDIDATES,
        shortages={"group_001": 0},
    )
    patch = build_prompt_failure(
        PromptFailureCode.PATCH_DEGRADES_OR_REQUIRES_UNSCORED_PATH,
        group_id="group_001",
    )

    assert "complete trajectory" in rejected["diagnosis"]
    assert "source_segment_id" in rejected["repair_requirement"]
    assert "source_segment_ids" not in rejected["repair_requirement"]
    assert "no valid complete trajectory" in exhausted["diagnosis"]
    assert "early-stop target" in exhausted["repair_requirement"]
    assert "group_001" in patch["diagnosis"]
    assert "whole trajectory" in patch["repair_requirement"]


def test_prompt_failure_requires_template_details() -> None:
    with pytest.raises(ValueError, match="missing_shot_count"):
        build_prompt_failure(
            PromptFailureCode.PROVIDER_DATA_INSPECTION_FAILED,
        )


def test_same_contract_rejects_unlisted_enum_and_extra_fields() -> None:
    package = _shot_package()
    valid_shot = {
        "shot_id": "shot_00001",
        "visual_description": "A woman crosses a room",
        "dominant_action": "Walking",
        "content_type": "pantomime",
        "narrative_function": "Shows purposeful movement",
        "emotional_tone": "determined",
        "emotional_intensity": 0.6,
        "scene": {
            "interior_exterior": "interior",
            "location": "apartment room",
            "time_of_day": "day",
            "environment_lighting": ["window light"],
            "color_palette": ["blue", "beige"],
            "color_tone": "muted",
            "set_details": ["window"],
            "weather": "",
            "atmosphere": "quiet",
        },
        "characters": [],
        "shot_scale": "close_up",
        "camera_angle": "eye_level",
        "camera_movement": "static",
        "composition": "subject centered in shallow depth",
        "visual_evidence": "The five frames show the same subject",
    }

    valid = {"shots": [valid_shot]}
    assert package.response_contract.validate_structure(valid) is valid

    with pytest.raises(ValueError, match=r"\$\.shots\[0\]\.shot_scale"):
        package.response_contract.validate_structure(
            {"shots": [{**valid_shot, "shot_scale": "medium_close_up"}]}
        )
    with pytest.raises(ValueError, match="Additional properties"):
        package.response_contract.validate_structure(
            {"shots": [{**valid_shot, "unexpected": True}]}
        )
