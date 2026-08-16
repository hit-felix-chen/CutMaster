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
    DialogueAnchorSelectionDetails,
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
    assert "distribute source_segment_ids as evenly as practical" in package.user_prompt
    assert "reserve sufficient chronological Segment space" in package.user_prompt
    assert package.prompt_version == "3.3"
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
            "planned_duration_sec": 3.2,
            "allowed_segment_ids": ["segment_0001", "segment_0002"],
        },
        "slot_04": {
            "desired_duration_sec": 4.0,
            "planned_duration_sec": 3.8,
            "allowed_segment_ids": ["segment_0003", "segment_0004"],
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
                    PromptFailureCode.INSUFFICIENT_NON_OVERLAPPING_CAPACITY,
                    slot_id="slot_04",
                    available_capacity=1,
                    candidates_needed=3,
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
            "planned_duration_sec"
        ]["const"]
        for schema in slots_schema["items"]["oneOf"]
    }
    assert planned_durations == {"slot_02": 3.2, "slot_04": 3.8}
    assert all(
        "planned_duration_sec" in schema["required"]
        for schema in slots_schema["items"]["oneOf"]
    )
    assert "single response" in package.user_prompt
    assert "authoritative visual clip duration" in package.user_prompt
    assert "Source quality and relevance to the maintained request always take priority" in (
        package.user_prompt
    )
    assert "distribute source_segment_ids as\nevenly as practical" in package.user_prompt
    assert "do not push a replacement toward an\ninterval boundary" in package.user_prompt
    assert package.prompt_version == "3.3"
    assert "<existing_slot_plan>" in package.user_prompt
    assert "<rejection_feedback>" in package.user_prompt


def test_dialogue_anchor_contract_selects_a_contiguous_range() -> None:
    package = prompt_registry.build(
        PromptStage.PLANNERS,
        PromptTask.DIALOGUE_ANCHOR_SELECTION,
        DialogueAnchorSelectionDetails(
            slots=[
                {
                    "slot_id": "slot_01",
                    "planned_duration_sec": 4.0,
                    "source_segment_ids": ["segment_0001"],
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
                    "min_audio_duration_sec": 1.0,
                    "max_audio_duration_sec": 60.0,
                    "planned_picture_duration_sec": 4.0,
                    "output_audio_start_sec": 0.0,
                }
            },
            max_anchors=3,
            min_anchor_duration_sec=1.0,
        ),
    )
    schema = package.response_contract.schema
    anchor = schema["properties"]["anchors"]["items"]["oneOf"][0]

    assert package.response_contract.version == "4.0"
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
    assert anchor["properties"]["first_dialogue_id"]["enum"] == ["7", "8"]
    assert anchor["properties"]["last_dialogue_id"]["enum"] == ["7", "8"]
    assert "<source_shots>" not in package.user_prompt
    assert "inclusive endpoints" in package.user_prompt
    assert "validates continuity" in package.user_prompt
    assert "same L-cut layout" in package.user_prompt
    assert "audio_cut_style" not in anchor["properties"]
    assert "<video_summary>" in package.user_prompt
    assert "<dialogue_constraints_by_slot>" in package.user_prompt
    assert "<valid_dialogue_ranges_by_slot>" not in package.user_prompt
    assert "Mia presses Sebastian for the truth." in package.user_prompt
    assert "<full_dialogue_context>" not in package.user_prompt
    assert "music_profile" not in package.context_keys
    assert "lip" not in package.user_prompt.lower()


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
            "Candidate is shorter than planned_duration_sec",
            "Duplicate candidate range for slot_01",
        )
    )

    assert '"attempt": 1' in retried.user_prompt
    assert '"attempt": 2' in retried.user_prompt
    assert "Candidate is shorter than planned_duration_sec" in retried.user_prompt
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
