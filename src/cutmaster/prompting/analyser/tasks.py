from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from cutmaster.prompting.core import (
    PromptModality,
    PromptPackage,
    PromptStage,
    PromptTask,
    ResponseContract,
    assemble_user_prompt,
)
from cutmaster.prompting.registry import prompt_registry
from cutmaster.contracts.video import (
    CameraAngle,
    CameraMovement,
    InteriorExterior,
    SegmentContentType,
    ShotScale,
    SpeechMode,
    TimeOfDay,
)


def _enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


@dataclass(frozen=True)
class DialogueReconstructionDetails:
    candidates: list[dict[str, Any]]
    operation: str


@dataclass(frozen=True)
class DialogueSegmentationDetails:
    dialogue_ids: list[int]


@dataclass(frozen=True)
class ShotAnnotationDetails:
    segment: dict[str, Any]
    shot: dict[str, Any]
    sampled_frame_times_sec: list[float]


@dataclass(frozen=True)
class SegmentSummaryDetails:
    segment: dict[str, Any]


@dataclass(frozen=True)
class VideoSummaryDetails:
    segment_ids: list[str]


def _dialogue_reconstruction(
    details: DialogueReconstructionDetails,
) -> PromptPackage:
    candidate_labels = [
        str(candidate["candidate_label"]) for candidate in details.candidates
    ]
    contract = ResponseContract(
        version="1.0",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["merge_candidate_ids"],
            "properties": {
                "merge_candidate_ids": {
                    "type": "array",
                    "uniqueItems": True,
                    "items": {
                        "type": "string",
                        "enum": candidate_labels,
                    },
                }
            },
        },
    )
    instructions = f"""# ASR dialogue-fragment reconstruction

The input contains candidate passages made only from adjacent cues by the same speaker.
Identify cue sequences that together form one grammatically and semantically complete spoken
sentence.

Rules:
1. Return only candidate labels whose complete cue sequence should actually be merged.
2. Omit candidates whose cues should remain separate.
3. Do not merge independent complete sentences, even when the speaker is unchanged.
4. Continuations split by ASR length limits, commas, clauses, numbers, or delayed sentence-final
   punctuation should be merged.
5. Do not rewrite text or timestamps.
6. Values such as cue_id are subtitle anchors. Never return a cue_id.

<candidates>
{json.dumps(details.candidates, ensure_ascii=False)}
</candidates>"""
    return PromptPackage(
        stage=PromptStage.ANALYSER,
        task=PromptTask.DIALOGUE_RECONSTRUCTION,
        prompt_version="1.0",
        operation=details.operation,
        system_prompt=(
            "You reconstruct complete spoken sentences from adjacent ASR subtitle fragments. "
            "Never rewrite dialogue and only return supplied candidate labels. Cue IDs are "
            "anchors, not candidate labels. Return strict JSON only."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=(),
        modality=PromptModality.TEXT,
    )


def _dialogue_segmentation(
    details: DialogueSegmentationDetails,
) -> PromptPackage:
    if not details.dialogue_ids:
        raise ValueError("Dialogue segmentation requires dialogue IDs")
    speech_modes = [
        value
        for value in _enum_values(SpeechMode)
        if value != SpeechMode.NONE.value
    ]
    contract = ResponseContract(
        version="1.0",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["segments"],
            "properties": {
                "segments": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "first_dialogue_id",
                            "last_dialogue_id",
                            "speech_mode",
                            "topic",
                            "summary",
                        ],
                        "properties": {
                            "first_dialogue_id": {
                                "type": "integer",
                                "enum": details.dialogue_ids,
                            },
                            "last_dialogue_id": {
                                "type": "integer",
                                "enum": details.dialogue_ids,
                            },
                            "speech_mode": {
                                "type": "string",
                                "enum": speech_modes,
                            },
                            "topic": {"type": "string", "minLength": 1},
                            "summary": {"type": "string", "minLength": 1},
                        },
                    },
                }
            },
        },
    )
    instructions = """Divide the complete transcript in the maintained context into contiguous
spoken-content Segments.

Rules:
1. Assign every dialogue_id exactly once and preserve source order.
2. A Segment contains one continuous conversation or one continuous monologue.
3. Return only inclusive first_dialogue_id/last_dialogue_id ranges.
4. Do not split adjacent lines when their covering_shot_ids overlap. A Segment boundary must
   fall between two PySceneDetect Shots.
5. Do not group unrelated dialogue across a narrative, speaker, topic, location, or large time
   break merely to reduce the number of Segments.
6. If a passage contains interaction between speakers, classify it as dialogue.
7. Topic and summary must be concise and grounded only in the supplied transcript. Participants
   are derived locally and must not be returned."""
    return PromptPackage(
        stage=PromptStage.ANALYSER,
        task=PromptTask.DIALOGUE_SEGMENTATION,
        prompt_version="1.0",
        operation="Full-transcript dialogue segmentation",
        system_prompt=(
            "You divide the complete source transcript into contiguous spoken-content Segments. "
            "Every dialogue line must be assigned exactly once and returned in source order. "
            "Segment boundaries must be compatible with supplied Shot memberships. Return "
            "strict JSON only."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=(
            "source_metadata",
            "shot_boundaries",
            "full_dialogue",
        ),
        modality=PromptModality.TEXT,
        output_artifact="dialogue_segments",
    )


def _shot_contract(
    shot_id: str,
    has_dialogue: bool,
) -> ResponseContract:
    content_types = (
        [SegmentContentType.NARRATIVE.value]
        if has_dialogue
        else [
            SegmentContentType.LANDSCAPE.value,
            SegmentContentType.EMOTIONAL.value,
            SegmentContentType.PANTOMIME.value,
        ]
    )
    character_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "character_id",
            "name",
            "description",
            "identity_likert",
            "identity_evidence",
            "screen_presence",
        ],
        "properties": {
            "character_id": {"type": "string", "minLength": 1},
            "name": {"type": "string", "minLength": 1},
            "description": {"type": "string", "minLength": 1},
            "identity_likert": {
                "type": "integer",
                "enum": [1, 2, 3, 4, 5],
            },
            "identity_evidence": {"type": "string", "minLength": 1},
            "screen_presence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
        },
    }
    scene_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "interior_exterior",
            "location",
            "time_of_day",
            "environment_lighting",
            "color_palette",
            "color_tone",
            "set_details",
            "weather",
            "atmosphere",
        ],
        "properties": {
            "interior_exterior": {
                "type": "string",
                "enum": _enum_values(InteriorExterior),
            },
            "location": {"type": "string", "minLength": 1},
            "time_of_day": {
                "type": "string",
                "enum": _enum_values(TimeOfDay),
            },
            "environment_lighting": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
            "color_palette": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "minLength": 1},
            },
            "color_tone": {"type": "string", "minLength": 1},
            "set_details": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
            },
            "weather": {"type": "string"},
            "atmosphere": {"type": "string", "minLength": 1},
        },
    }
    return ResponseContract(
        version="2.0",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "shot_id",
                "visual_description",
                "dominant_action",
                "content_type",
                "narrative_function",
                "emotional_tone",
                "emotional_intensity",
                "scene",
                "characters",
                "shot_scale",
                "camera_angle",
                "camera_movement",
                "composition",
                "visual_evidence",
            ],
            "properties": {
                "shot_id": {"type": "string", "const": shot_id},
                "visual_description": {"type": "string", "minLength": 1},
                "dominant_action": {"type": "string", "minLength": 1},
                "content_type": {"type": "string", "enum": content_types},
                "narrative_function": {"type": "string", "minLength": 1},
                "emotional_tone": {"type": "string", "minLength": 1},
                "emotional_intensity": {
                    "type": "number",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
                "scene": scene_schema,
                "characters": {
                    "type": "array",
                    "items": character_schema,
                },
                "shot_scale": {
                    "type": "string",
                    "enum": _enum_values(ShotScale),
                },
                "camera_angle": {
                    "type": "string",
                    "enum": _enum_values(CameraAngle),
                },
                "camera_movement": {
                    "type": "string",
                    "enum": _enum_values(CameraMovement),
                },
                "composition": {"type": "string", "minLength": 1},
                "visual_evidence": {"type": "string", "minLength": 1},
            },
        },
    )


def _shot_annotation(details: ShotAnnotationDetails) -> PromptPackage:
    shot = details.shot
    segment = details.segment
    has_dialogue = bool(shot["dialogue"])
    contract = _shot_contract(str(shot["shot_id"]), has_dialogue)
    instructions = f"""Annotate exactly one Shot from the five attached frames, shown in
chronological order and sampled uniformly inside the Shot.

The maintained full transcript is global context for names and narrative position only. It is
not evidence that a person, action, object, location, or emotion is visible. Pixel evidence
always wins. If a visible person's identity cannot be established, assign a stable generic name
such as person_01 instead of guessing a cast identity.

This Shot has_dialogue={str(has_dialogue).lower()}. Therefore content_type must be narrative when
has_dialogue is true; otherwise choose landscape, emotional, or pantomime.

Identity Likert:
1 = identity cannot be established from these frames;
2 = weak person-specific evidence;
3 = plausible identity with partial facial evidence;
4 = clear facial match in a meaningful portion;
5 = repeated, unmistakable facial match.

Choose one dominant camera scale, angle, and movement. Do not return mixed, other, unknown, or a
new intermediate label. Standard medium close-up/head-and-shoulders framing belongs to close_up;
medium_close_up is not an allowed value. Describe locations concretely from visible structure.

<segment>
{json.dumps({
    "segment_id": segment["segment_id"],
    "has_dialogue": segment["has_dialogue"],
    "speech_mode": segment["speech_mode"],
    "dialogue_context": segment["dialogue_context"],
}, ensure_ascii=False)}
</segment>

<shot>
{json.dumps({
    "shot_id": shot["shot_id"],
    "timestamp": shot["timestamp"],
    "dialogue": shot["dialogue"],
    "sampled_frame_times_sec": details.sampled_frame_times_sec,
}, ensure_ascii=False)}
</shot>"""
    return PromptPackage(
        stage=PromptStage.ANALYSER,
        task=PromptTask.SHOT_ANNOTATION,
        prompt_version="2.0",
        operation=f"Shot visual annotation {shot['shot_id']}",
        system_prompt=(
            "You annotate exactly one source-video Shot from five uniformly sampled frames. "
            "The complete transcript is global narrative context, never visual evidence. "
            "Describe only visible people, actions, locations, lighting, colors, objects, and "
            "camera properties. Every categorical value must exactly match the response "
            "contract. Return strict JSON only."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=("source_metadata", "full_dialogue"),
        modality=PromptModality.TEXT_AND_IMAGES,
    )


def _segment_summary(details: SegmentSummaryDetails) -> PromptPackage:
    segment_id = str(details.segment["segment_id"])
    contract = ResponseContract(
        version="1.0",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["segment_id", "segment_summary"],
            "properties": {
                "segment_id": {
                    "type": "string",
                    "const": segment_id,
                },
                "segment_summary": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 600,
                },
            },
        },
    )
    instructions = f"""Summarize exactly one source-video Segment as a concise reusable event
description for later editing decisions.

Integrate the supplied dialogue context, exact dialogue occurrences, and Shot annotations into
one to three sentences. State who does what, the meaningful interaction or change, and the
immediate narrative significance when supported. Preserve source chronology.

Do not enumerate Shots or dialogue lines. Do not mention camera metadata, annotation failures,
ASR, prompts, or editing. Do not infer events from dialogue when the visual annotations contradict
them. Shots marked provider_rejected have no visual evidence and must not be described as if they
were visually annotated.

<segment>
{json.dumps(details.segment, ensure_ascii=False)}
</segment>"""
    return PromptPackage(
        stage=PromptStage.ANALYSER,
        task=PromptTask.SEGMENT_SUMMARY,
        prompt_version="1.0",
        operation=f"Segment summary {segment_id}",
        system_prompt=(
            "You create a concise, factual Segment-level summary from structured dialogue and "
            "Shot annotations. Return strict JSON only."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=(),
        modality=PromptModality.TEXT,
    )


def _video_summary(details: VideoSummaryDetails) -> PromptPackage:
    contract = ResponseContract(
        version="1.0",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": [
                "schema_version",
                "title",
                "logline",
                "synopsis",
                "chronological_story_beats",
                "character_arcs",
                "themes",
                "ending",
            ],
            "properties": {
                "schema_version": {"type": "string", "const": "1.0"},
                "title": {"type": "string", "minLength": 1},
                "logline": {"type": "string", "minLength": 1},
                "synopsis": {"type": "string", "minLength": 1},
                "chronological_story_beats": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "source_segment_ids",
                            "summary",
                            "characters",
                            "narrative_significance",
                        ],
                        "properties": {
                            "source_segment_ids": {
                                "type": "array",
                                "minItems": 1,
                                "uniqueItems": True,
                                "items": {
                                    "type": "string",
                                    "enum": details.segment_ids,
                                },
                            },
                            "summary": {"type": "string", "minLength": 1},
                            "characters": {
                                "type": "array",
                                "uniqueItems": True,
                                "items": {"type": "string", "minLength": 1},
                            },
                            "narrative_significance": {
                                "type": "string",
                                "minLength": 1,
                            },
                        },
                    },
                },
                "character_arcs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "character",
                            "arc",
                            "key_segment_ids",
                        ],
                        "properties": {
                            "character": {"type": "string", "minLength": 1},
                            "arc": {"type": "string", "minLength": 1},
                            "key_segment_ids": {
                                "type": "array",
                                "uniqueItems": True,
                                "items": {
                                    "type": "string",
                                    "enum": details.segment_ids,
                                },
                            },
                        },
                    },
                },
                "themes": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string", "minLength": 1},
                },
                "ending": {"type": "string", "minLength": 1},
            },
        },
    )
    instructions = """Create a reusable, grounded story summary after all source-video Segments
have been annotated.

The maintained video_summary_context contains every Segment field except the internal shots list,
plus the complete chronological ASR transcript. Treat Segment summaries as visual source truth
and the transcript as spoken narrative context. Integrate visible actions, dialogue meaning,
character relationships, turning points, consequences, and ending. Resolve the story
chronologically. Give enough context for later editing models to understand why events and lines
matter without receiving Shot-level annotations again.

Do not reproduce the transcript, enumerate individual dialogue lines, infer events absent from
the annotations, or describe editing choices. The synopsis should read as a coherent account of
the whole work. Story beats must cite the exact source Segment IDs that support them. Character
arcs must distinguish the major characters and explain meaningful change over time."""
    return PromptPackage(
        stage=PromptStage.ANALYSER,
        task=PromptTask.VIDEO_SUMMARY,
        prompt_version="2.0",
        operation="Full-video story summarization",
        system_prompt=(
            "You synthesize reusable story understanding from Segment-level video descriptions "
            "and the complete ASR transcript. Ground every statement in the supplied structure "
            "and return strict JSON only."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=("source_metadata", "video_summary_context"),
        modality=PromptModality.TEXT,
        output_artifact="video_summary",
    )


prompt_registry.register(
    PromptStage.ANALYSER,
    PromptTask.DIALOGUE_RECONSTRUCTION,
    _dialogue_reconstruction,
)
prompt_registry.register(
    PromptStage.ANALYSER,
    PromptTask.DIALOGUE_SEGMENTATION,
    _dialogue_segmentation,
)
prompt_registry.register(
    PromptStage.ANALYSER,
    PromptTask.SHOT_ANNOTATION,
    _shot_annotation,
)
prompt_registry.register(
    PromptStage.ANALYSER,
    PromptTask.SEGMENT_SUMMARY,
    _segment_summary,
)
prompt_registry.register(
    PromptStage.ANALYSER,
    PromptTask.VIDEO_SUMMARY,
    _video_summary,
)
