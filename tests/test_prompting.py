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
            hard_forbidden_assignments={
                "slot_02": [["segment_0002"]],
            },
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
    assert "source Segment must be longer" in package.user_prompt
    assert "alternative candidates may overlap" in package.user_prompt
    assert (
        '<hard_forbidden_assignments>\n{"slot_02": [["segment_0002"]]}\n'
        '</hard_forbidden_assignments>'
    ) in package.user_prompt
    assert "same slot_id must not repeat" in package.user_prompt
    assert "first array item is slot_01" in package.user_prompt
    assert package.prompt_version == "3.5"
    assert package.context_keys == (
        "request",
        "music_profile",
        "source_story_context",
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
    assert "Collateral/blocker Slots" in package.user_prompt
    assert "keep their previous_source_segment_ids" in package.user_prompt
    assert "strictly increasing source order" in package.user_prompt
    assert package.prompt_version == "3.6"
    assert "<existing_slot_plan>" in package.user_prompt
    assert "<rejection_feedback>" in package.user_prompt


def test_targeted_slot_arrangement_contract_preserves_requirements_by_failure_kind() -> None:
    package = prompt_registry.build(
        PromptStage.PLANNERS,
        PromptTask.SLOT_ARRANGEMENT,
        SlotArrangementDetails(
            target_duration_sec=8.0,
            target_clip_duration_sec=4.0,
            allowed_segment_ids=[],
            retry_note="",
            mode="targeted",
            existing_slots=[{"slot_id": "slot_01"}],
            target_slot_constraints={
                "slot_01": {
                    "desired_duration_sec": 4.0,
                    "planned_duration_sec": 4.0,
                    "allowed_segment_ids": ["segment_0001", "segment_0002"],
                }
            },
            rejection_feedback=[
                build_prompt_failure(
                    PromptFailureCode.REQUIRED_SUBJECT_NOT_VISUALLY_CONFIRMED,
                    slot_id="slot_01",
                    candidate_id="candidate_01",
                    timestamp="00:00:01,000-00:00:05,000",
                    required_visible_subjects=["goalkeeper"],
                    visual_evidence="The goalkeeper is outside the selected window.",
                )
            ],
        ),
    )

    prompt_text = " ".join(package.user_prompt.split())
    assert "Local repair never changes required_visible_subjects" in prompt_text
    assert "keep the original required_visible_subjects" in prompt_text
    assert "visually_static" in prompt_text
    assert "source_segments_too_short" in prompt_text
    assert "visual_slot_not_relevant" in prompt_text


def test_candidate_retrieval_uses_each_slot_as_the_only_editorial_target() -> None:
    package = prompt_registry.build(
        PromptStage.PLANNERS,
        PromptTask.CANDIDATE_RETRIEVAL,
        CandidateRetrievalDetails(
            operation="Candidate retrieval round 1 slot slot_01",
            candidates_per_slot=1,
            slots=[
                {
                    "slot_id": "slot_01",
                    "content_description": "A child fan carries a trophy toward the stadium.",
                    "required_visible_subjects": ["child fan", "trophy"],
                    "planned_duration_sec": 4.0,
                    "source_segment_ids": ["segment_0001"],
                }
            ],
            confirmed_candidates={"slot_01": []},
            excluded_ranges={"slot_01": []},
            source_segments_by_slot={
                "slot_01": [
                    {
                        "segment_id": "segment_0001",
                        "time_range": {"start_sec": 0.0, "end_sec": 10.0},
                        "shots": [],
                    }
                ]
            },
        ),
    )

    assert package.prompt_version == "2.3"
    assert package.response_contract.version == "1.2"
    assert package.context_keys == ("video_summary",)
    assert "sole editorial target" in package.user_prompt
    assert "Slot contract" in package.user_prompt
    assert "maintained request" not in package.user_prompt
    prompt_text = " ".join(package.user_prompt.split())
    assert "different source Shots are independent" in prompt_text
    assert "Small timestamp displacements or overlapping windows" in prompt_text
    assert "same Shot is allowed when its time window does not overlap" in prompt_text
    assert "candidates for the same Slot may overlap" not in package.user_prompt


def test_candidate_visual_scoring_is_strictly_slot_local_with_legacy_shape() -> None:
    package = prompt_registry.build(
        PromptStage.PLANNERS,
        PromptTask.CANDIDATE_VISUAL_SCORING,
        CandidateVisualScoringDetails(
            operation="Visual candidate validation round 1 slot slot_01",
            candidates=[
                {
                    "candidate_id": "candidate_01",
                    "slot_id": "slot_01",
                    "intended_visible_content": (
                        "A child fan carries a trophy toward the stadium."
                    ),
                    "required_visible_subjects": ["child fan", "trophy"],
                    "source_segment_video_descriptions": [],
                }
            ],
        ),
    )
    item_schema = package.response_contract.schema["properties"]["items"]["items"]

    assert package.prompt_version == "2.1"
    assert package.response_contract.version == "2.2"
    assert package.context_keys == ()
    assert item_schema["required"] == [
        "candidate_id",
        "visible_description",
        "visible_subjects",
        "required_subject_visibility",
        "visual_slot_relevance",
        "visual_evidence",
    ]
    assert set(item_schema["properties"]) == set(item_schema["required"])
    prompt_text = " ".join(
        (package.system_prompt + "\n" + package.user_prompt).split()
    )
    assert "weakest required subject" in prompt_text
    assert "current Slot's required_visible_subjects" in prompt_text
    assert "only against intended_visible_content" in prompt_text
    assert "maintained request" not in prompt_text
    assert "requested focal subject" not in prompt_text
    assert "source title" not in prompt_text
    assert "match_mode" not in prompt_text
    assert "match_mode" not in json.dumps(package.response_contract.schema)


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


def test_visual_slot_relevance_failure_has_independent_repair_requirement() -> None:
    failure = build_prompt_failure(
        PromptFailureCode.VISUAL_SLOT_NOT_RELEVANT,
        candidate_id="candidate_01",
        timestamp="00:00:01,000-00:00:05,000",
        visual_slot_relevance_likert=2,
        visual_slot_relevance_likert_threshold=3,
        visual_evidence="The goalkeeper is visible but no save occurs.",
    )

    assert failure["reason_code"] == "visual_slot_not_relevant"
    assert "Subject presence alone" in failure["repair_requirement"]


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
