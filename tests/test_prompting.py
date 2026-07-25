from __future__ import annotations

import json

import pytest

from cutmaster.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.prompting.analyser import ShotAnnotationDetails
from cutmaster.prompting.core import response_template_from_schema


def _shot_package():
    return prompt_registry.build(
        PromptStage.ANALYSER,
        PromptTask.SHOT_ANNOTATION,
        ShotAnnotationDetails(
            segment={
                "segment_id": "segment_0001",
                "has_dialogue": False,
                "speech_mode": "none",
                "dialogue_context": None,
            },
            shot={
                "shot_id": "shot_00001",
                "timestamp": "00:00:00,000-00:00:01,000",
                "dialogue": [],
            },
            sampled_frame_times_sec=[0.1, 0.3, 0.5, 0.7, 0.9],
        ),
    )


def test_registry_exposes_every_model_task() -> None:
    assert set(prompt_registry.registered_keys()) == {
        (PromptStage.ANALYSER, PromptTask.DIALOGUE_RECONSTRUCTION),
        (PromptStage.ANALYSER, PromptTask.DIALOGUE_SEGMENTATION),
        (PromptStage.ANALYSER, PromptTask.SHOT_ANNOTATION),
        (PromptStage.PLANNER, PromptTask.SLOT_PLANNING),
        (PromptStage.PLANNER, PromptTask.CANDIDATE_RETRIEVAL),
        (PromptStage.PLANNER, PromptTask.CANDIDATE_VISUAL_SCORING),
        (PromptStage.PLANNER, PromptTask.PAIRWISE_SCORING),
        (PromptStage.PLANNER, PromptTask.SCRIPT_REVIEW),
    }


def test_prompt_embeds_contract_and_template_derived_from_same_schema() -> None:
    package = _shot_package()
    schema = package.response_contract.schema
    template = response_template_from_schema(schema)

    assert json.dumps(schema, ensure_ascii=False, indent=2) in package.user_prompt
    assert json.dumps(template, ensure_ascii=False, indent=2) in package.user_prompt
    assert len(package.response_contract.fingerprint) == 16
    assert len(package.fingerprint) == 16


def test_same_contract_rejects_unlisted_enum_and_extra_fields() -> None:
    package = _shot_package()
    valid = {
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

    assert package.response_contract.validate_structure(valid) is valid

    with pytest.raises(ValueError, match=r"\$\.shot_scale"):
        package.response_contract.validate_structure(
            {**valid, "shot_scale": "medium_close_up"}
        )
    with pytest.raises(ValueError, match="Additional properties"):
        package.response_contract.validate_structure(
            {**valid, "unexpected": True}
        )
