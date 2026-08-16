from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from cutmaster.workflow.prompting.core import (
    PromptModality,
    PromptPackage,
    PromptStage,
    PromptTask,
    ResponseContract,
    assemble_user_prompt,
)
from cutmaster.workflow.prompting.registry import prompt_registry
from cutmaster.workflow.contracts.video import (
    CameraAngle,
    CameraMovement,
    InteriorExterior,
    SegmentContentType,
    ShotScale,
    TimeOfDay,
    TimelineRole,
)


SCENE_BOUNDARY_PROMPT_VERSION = "1.0"


def _enum_values(enum_type: type[StrEnum]) -> list[str]:
    return [member.value for member in enum_type]


@dataclass(frozen=True)
class DialogueReconstructionDetails:
    candidates: list[dict[str, Any]]
    operation: str


@dataclass(frozen=True)
class SceneBoundaryDetectionDetails:
    window_id: str
    shots: list[dict[str, Any]]
    focus_shot_ids: list[str]


@dataclass(frozen=True)
class SegmentShotAnnotationDetails:
    segment: dict[str, Any]
    sampled_frame_times_by_shot: dict[str, list[float]]
    frame_delivery: str = "individual"
    batch_index: int = 1
    batch_count: int = 1
    total_segment_shots: int | None = None


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


def _scene_boundary_detection(
    details: SceneBoundaryDetectionDetails,
) -> PromptPackage:
    if not details.shots or not details.focus_shot_ids:
        raise ValueError("Scene boundary detection requires context and focus Shots")
    context_shot_ids = [str(shot["shot_id"]) for shot in details.shots]
    if not set(details.focus_shot_ids).issubset(context_shot_ids):
        raise ValueError("Every focus Shot must belong to the context window")
    contract = ResponseContract(
        version="1.0",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["decisions"],
            "properties": {
                "decisions": {
                    "type": "array",
                    "minItems": len(details.focus_shot_ids),
                    "maxItems": len(details.focus_shot_ids),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "shot_id",
                            "is_scene_end",
                            "confidence_likert",
                        ],
                        "properties": {
                            "shot_id": {
                                "type": "string",
                                "enum": details.focus_shot_ids,
                            },
                            "is_scene_end": {"type": "boolean"},
                            "confidence_likert": {
                                "type": "integer",
                                "enum": [1, 2, 3, 4, 5],
                            },
                        },
                    },
                }
            },
        },
    )
    textual_shots = [
        {
            "shot_id": str(shot["shot_id"]),
            "timestamp": str(shot["timestamp"]),
            "dialogue": [
                {
                    "dialogue_id": int(item["dialogue_id"]),
                    "speaker": str(item["speaker"]),
                    "text": str(item["text"]),
                    "timestamp": str(item["timestamp"]),
                }
                for item in shot.get("dialogue") or []
            ],
        }
        for shot in details.shots
    ]
    instructions = f"""# Scene-VLM sequential Scene-boundary detection

A Scene is a consecutive sequence of Shots that remains semantically coherent in location,
time, characters, action, dialogue, or narrative purpose. Decide whether each focus Shot is the
final Shot of its current Scene. A camera cut alone is not necessarily a Scene boundary.

The context contains {len(details.shots)} chronological Shots. The attached images are ordered
exactly like <context_shots>, with three chronological images per Shot. Every image has its Shot
ID visibly overlaid in the top-left corner. Dialogue is synchronized context, but it is not visual
evidence.

Rules:
1. Return one decision for every focus Shot, in the exact supplied focus order.
2. is_scene_end=true means the next Shot begins a semantically different Scene.
3. Consider visual continuity, time and location, recurring people, ongoing action, dialogue
   continuity, and narrative purpose together.
4. A change of camera angle, shot scale, or speaker inside one continuous event is not a Scene
   boundary.
5. A cross-cut may remain part of one Scene when it continues the same narrative event.
6. Do not return decisions for context-only Shots.
7. confidence_likert measures confidence in the selected Yes/No decision: 1 is very uncertain,
   3 is moderately confident, and 5 is unmistakable.
8. Return no rationale, summary, invented label, timestamp, or additional field.

<window_id>{details.window_id}</window_id>
<context_shots>
{json.dumps(textual_shots, ensure_ascii=False)}
</context_shots>
<focus_shot_ids>
{json.dumps(details.focus_shot_ids, ensure_ascii=False)}
</focus_shot_ids>"""
    return PromptPackage(
        stage=PromptStage.ANALYSER,
        task=PromptTask.SCENE_BOUNDARY_DETECTION,
        prompt_version=SCENE_BOUNDARY_PROMPT_VERSION,
        operation=f"Scene boundary detection {details.window_id}",
        system_prompt=(
            "You perform sequential movie Scene-boundary classification over consecutive Shots. "
            "Use the attached frames and synchronized dialogue together. Return strict JSON "
            "containing only the requested focus-Shot decisions."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=(),
        modality=PromptModality.TEXT_AND_IMAGES,
        output_artifact="scene_boundary_decisions",
    )


def _shot_schema(
    shot_id: str,
    has_dialogue: bool,
) -> dict[str, Any]:
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
    return {
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
    }


def _shot_annotation(details: SegmentShotAnnotationDetails) -> PromptPackage:
    segment = details.segment
    shots = segment["shots"]
    if not shots:
        raise ValueError("Segment Shot annotation requires at least one Shot")
    if details.frame_delivery not in {"individual", "contact_sheet"}:
        raise ValueError(
            "Segment Shot annotation frame_delivery must be individual or "
            "contact_sheet"
        )
    if not 1 <= details.batch_index <= details.batch_count:
        raise ValueError("Segment Shot annotation batch position is invalid")
    if details.batch_count > 1 and details.total_segment_shots is None:
        raise ValueError(
            "Batched Segment Shot annotation requires total_segment_shots"
        )
    contract = ResponseContract(
        version="3.0",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["shots"],
            "properties": {
                "shots": {
                    "type": "array",
                    "minItems": len(shots),
                    "maxItems": len(shots),
                    "prefixItems": [
                        _shot_schema(
                            str(shot["shot_id"]),
                            bool(shot["dialogue"]),
                        )
                        for shot in shots
                    ],
                    "items": False,
                }
            },
        },
    )
    shot_context = [
        {
            "shot_id": shot["shot_id"],
            "timestamp": shot["timestamp"],
            "has_dialogue": bool(shot["dialogue"]),
            "dialogue": shot["dialogue"],
            "sampled_frame_times_sec": details.sampled_frame_times_by_shot[
                str(shot["shot_id"])
            ],
        }
        for shot in shots
    ]
    image_delivery = (
        "The attached\n"
        "images are in chronological Shot-major order, with exactly five uniformly sampled frames for\n"
        "each Shot. Frame labels identify their Shot and position."
        if details.frame_delivery == "individual"
        else (
            "The attached images are in chronological Shot order, with one contact sheet "
            "per Shot. Each contact sheet contains exactly five uniformly sampled frames "
            "numbered 1 through 5. Image labels identify the corresponding Shot."
        )
    )
    batch_note = ""
    if details.batch_count > 1:
        batch_note = f"""

This is request batch {details.batch_index} of {details.batch_count} for the same semantic
Segment, which contains {details.total_segment_shots} Shots in total. Annotate only the supplied
chronological subset. The caller will merge all batches into the original Segment and preserve
the complete source Shot order."""
    instructions = f"""Annotate every Shot in exactly one source-video Segment. {image_delivery}{batch_note}

The maintained full transcript is global context for names and narrative position only. It is
not evidence that a person, action, object, location, or emotion is visible. Pixel evidence
always wins. If a visible person's identity cannot be established, assign a stable generic name
such as person_01 instead of guessing a cast identity.

Return one annotation for every supplied Shot in the exact source order. For each Shot,
content_type must be narrative when has_dialogue is true; otherwise choose landscape, emotional,
or pantomime. When the same visible identity recurs across Shots, keep its character_id and name
consistent within this Segment.

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
}, ensure_ascii=False)}
</segment>

<shots>
{json.dumps(shot_context, ensure_ascii=False)}
</shots>"""
    return PromptPackage(
        stage=PromptStage.ANALYSER,
        task=PromptTask.SHOT_ANNOTATION,
        prompt_version="3.0",
        operation=(
            f"Segment Shot visual annotation {segment['segment_id']}"
            if details.batch_count == 1
            else (
                f"Segment Shot visual annotation {segment['segment_id']} "
                f"batch {details.batch_index}/{details.batch_count}"
            )
        ),
        system_prompt=(
            "You annotate every source-video Shot in one Segment from five uniformly sampled "
            "frames per Shot. "
            "The complete transcript is global narrative context, never visual evidence. "
            "Describe only visible people, actions, locations, lighting, colors, objects, and "
            "camera properties. Preserve Shot order and return a strict JSON shots array whose "
            "categorical values exactly match the response contract."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=("source_metadata", "full_dialogue"),
        modality=PromptModality.TEXT_AND_IMAGES,
    )


def _segment_summary(details: SegmentSummaryDetails) -> PromptPackage:
    segment_id = str(details.segment["segment_id"])
    contract = ResponseContract(
        version="2.0",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["segment_id", "segment_summary", "timeline_role"],
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
                "timeline_role": {
                    "type": "string",
                    "enum": _enum_values(TimelineRole),
                },
            },
        },
    )
    instructions = f"""Summarize exactly one source-video Segment as a concise reusable event
description for later editing decisions.

Integrate the supplied exact dialogue occurrences and Shot annotations into
one to three sentences. State who does what, the meaningful interaction or change, and the
immediate narrative significance when supported. Preserve source chronology.

Do not enumerate Shots or dialogue lines. Do not mention camera metadata, annotation failures,
ASR, prompts, or editing. Do not infer events from dialogue when the visual annotations contradict
them. Shots marked provider_rejected have no visual evidence and must not be described as if they
were visually annotated.

Classify timeline_role from the Segment's actual narrative function, not from its ordinal source
position. opening introduces a story situation, body develops or continues it, and ending provides
closure or aftermath. These are reusable semantic labels rather than a mandatory three-part
template: every label may appear multiple times or not appear at all.

<segment>
{json.dumps(details.segment, ensure_ascii=False)}
</segment>"""
    return PromptPackage(
        stage=PromptStage.ANALYSER,
        task=PromptTask.SEGMENT_SUMMARY,
        prompt_version="2.0",
        operation=f"Segment summary {segment_id}",
        system_prompt=(
            "You create a concise, factual Segment-level summary and classify its semantic "
            "timeline role from structured dialogue and Shot annotations. Return strict JSON "
            "only."
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
    PromptTask.SCENE_BOUNDARY_DETECTION,
    _scene_boundary_detection,
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
