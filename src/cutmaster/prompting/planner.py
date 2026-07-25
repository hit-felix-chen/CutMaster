from __future__ import annotations

import json
from dataclasses import dataclass
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


SCORE_SCHEMA = {
    "type": "number",
    "minimum": 0.0,
    "maximum": 1.0,
}


@dataclass(frozen=True)
class SlotPlanningDetails:
    clip_count: int
    target_duration_sec: float
    allowed_segment_ids: list[str]
    retry_note: str


@dataclass(frozen=True)
class CandidateRetrievalDetails:
    operation: str
    candidates_per_slot: int
    slots: list[dict[str, Any]]
    excluded_ranges: dict[str, list[str]]
    source_segments_by_slot: dict[str, list[dict[str, Any]]]


@dataclass(frozen=True)
class CandidateVisualScoringDetails:
    operation: str
    candidates: list[dict[str, Any]]


@dataclass(frozen=True)
class PairwiseScoringDetails:
    operation: str
    previous_slot: dict[str, Any]
    current_slot: dict[str, Any]
    pair_specs: list[dict[str, Any]]


@dataclass(frozen=True)
class ScriptReviewDetails:
    slots: list[dict[str, Any]]
    candidate_pool: dict[str, list[dict[str, Any]]]


def _slot_planning(details: SlotPlanningDetails) -> PromptPackage:
    contract = ResponseContract(
        version="1.0",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["slots"],
            "properties": {
                "slots": {
                    "type": "array",
                    "minItems": details.clip_count,
                    "maxItems": details.clip_count,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "narrative_role",
                            "content_description",
                            "target_emotion",
                            "target_emotional_intensity",
                            "target_kinetic_energy",
                            "desired_duration_sec",
                            "continuity_from_previous",
                            "source_segment_ids",
                            "required_visible_subjects",
                        ],
                        "properties": {
                            "narrative_role": {
                                "type": "string",
                                "enum": [
                                    "setup",
                                    "development",
                                    "turning_point",
                                    "climax",
                                    "resolution",
                                ],
                            },
                            "content_description": {
                                "type": "string",
                                "minLength": 1,
                            },
                            "target_emotion": {
                                "type": "string",
                                "minLength": 1,
                            },
                            "target_emotional_intensity": SCORE_SCHEMA,
                            "target_kinetic_energy": SCORE_SCHEMA,
                            "desired_duration_sec": {
                                "type": "number",
                                "exclusiveMinimum": 0.0,
                            },
                            "continuity_from_previous": {
                                "type": "string",
                                "minLength": 1,
                            },
                            "source_segment_ids": {
                                "type": "array",
                                "minItems": 1,
                                "uniqueItems": True,
                                "items": {
                                    "type": "string",
                                    "enum": details.allowed_segment_ids,
                                },
                            },
                            "required_visible_subjects": {
                                "type": "array",
                                "uniqueItems": True,
                                "items": {"type": "string", "minLength": 1},
                            },
                        },
                    },
                }
            },
        },
    )
    retry_note = f"\n{details.retry_note.strip()}\n" if details.retry_note else ""
    instructions = f"""Create exactly {details.clip_count} sequential edit slots for the
maintained request, music profile, and structured video description.

Each slot must be realizable from supplied source_segment_ids. Use Shot-level VLM descriptions,
characters, scenes, dialogue, and Segment summaries as source truth. Never invent props,
gestures, settings, identities, or actions absent from the description. Keep source_segment_ids
in nondecreasing source order across slots. Reusing a Segment for adjacent slots is allowed when
it contains enough distinct Shots.

For a character-focused request, list the focal character in required_visible_subjects whenever
that character must be seen. Use music sections and energy to vary duration: high kinetic energy
generally uses shorter clips and low energy uses longer clips. Maintain a coherent progression.
Every slot after the first must explain how it continues or contrasts with the previous slot.
Desired durations should total approximately {details.target_duration_sec:.1f} seconds.
{retry_note}"""
    return PromptPackage(
        stage=PromptStage.PLANNER,
        task=PromptTask.SLOT_PLANNING,
        prompt_version="1.0",
        operation="Edit slot planning",
        system_prompt=(
            "You are the planning component of a professional video editor. Plan an output "
            "timeline but do not select source timestamps. Return strict JSON only."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=(
            "request",
            "music_profile",
            "video_description",
            "planning_feedback",
        ),
        modality=PromptModality.TEXT,
        output_artifact="edit_plan_unaligned",
    )


def _candidate_item_schema(available_shot_ids: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "timestamp",
            "source_shot_ids",
            "description",
            "matched_dialogue",
            "semantic_relevance",
            "emotional_intensity",
            "salience",
        ],
        "properties": {
            "timestamp": {
                "type": "string",
                "pattern": (
                    r"^\d{2}:\d{2}:\d{2},\d{3}-"
                    r"\d{2}:\d{2}:\d{2},\d{3}$"
                ),
            },
            "source_shot_ids": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "enum": available_shot_ids,
                },
            },
            "description": {"type": "string", "minLength": 1},
            "matched_dialogue": {"type": "string"},
            "semantic_relevance": SCORE_SCHEMA,
            "emotional_intensity": SCORE_SCHEMA,
            "salience": SCORE_SCHEMA,
        },
    }


def _candidate_retrieval(details: CandidateRetrievalDetails) -> PromptPackage:
    slot_group_schemas = []
    for slot in details.slots:
        slot_id = str(slot["slot_id"])
        available_shot_ids = [
            str(shot["shot_id"])
            for segment in details.source_segments_by_slot[slot_id]
            for shot in segment["shots"]
        ]
        slot_group_schemas.append(
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["slot_id", "items"],
                "properties": {
                    "slot_id": {"type": "string", "const": slot_id},
                    "items": {
                        "type": "array",
                        "minItems": details.candidates_per_slot,
                        "maxItems": details.candidates_per_slot,
                        "items": _candidate_item_schema(available_shot_ids),
                    },
                },
            }
        )
    contract = ResponseContract(
        version="1.0",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["candidates"],
            "properties": {
                "candidates": {
                    "type": "array",
                    "minItems": len(details.slots),
                    "maxItems": len(details.slots),
                    "items": {"oneOf": slot_group_schemas},
                }
            },
        },
    )
    instructions = f"""Retrieve exactly {details.candidates_per_slot} distinct source candidates
for every supplied edit Slot.

Use only supplied structured source Segments and Shot-level visual annotations. Every candidate
must consist of one or more consecutive source_shot_ids. Its timestamp must exactly equal the
start boundary of its first Shot and the end boundary of its last Shot. Its duration must be at
least planned_duration_sec. Silent Segments are valid source material.

Prefer each Slot's source_segment_ids, preserve source chronology, and avoid every excluded
range. Candidate descriptions must summarize supplied visual Shot descriptions. Dialogue may
support narrative meaning but must not override visible identity or action. Score semantic
relevance, emotional intensity, and editorial salience from 0 to 1.

<slots>
{json.dumps(details.slots, ensure_ascii=False)}
</slots>
<excluded_ranges>
{json.dumps(details.excluded_ranges, ensure_ascii=False)}
</excluded_ranges>
<available_source_segments_by_slot>
{json.dumps(details.source_segments_by_slot, ensure_ascii=False)}
</available_source_segments_by_slot>"""
    return PromptPackage(
        stage=PromptStage.PLANNER,
        task=PromptTask.CANDIDATE_RETRIEVAL,
        prompt_version="1.0",
        operation=details.operation,
        system_prompt=(
            "You retrieve real source-video passages from a structured VideoDescription whose "
            "Segment and Shot boundaries are authoritative. Never invent timestamps, Shots, "
            "visuals, or dialogue. Return strict JSON only."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=("request",),
        modality=PromptModality.TEXT,
    )


def _candidate_visual_scoring(
    details: CandidateVisualScoringDetails,
) -> PromptPackage:
    candidate_ids = [
        str(candidate["candidate_id"]) for candidate in details.candidates
    ]
    contract = ResponseContract(
        version="1.0",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "minItems": len(candidate_ids),
                    "maxItems": len(candidate_ids),
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "candidate_id",
                            "visible_description",
                            "visible_subjects",
                            "required_subject_visibility",
                            "visual_slot_relevance",
                            "visual_evidence",
                        ],
                        "properties": {
                            "candidate_id": {
                                "type": "string",
                                "enum": candidate_ids,
                            },
                            "visible_description": {
                                "type": "string",
                                "minLength": 1,
                            },
                            "visible_subjects": {
                                "type": "array",
                                "uniqueItems": True,
                                "items": {"type": "string", "minLength": 1},
                            },
                            "required_subject_visibility": {
                                "type": "integer",
                                "enum": [1, 2, 3, 4, 5],
                            },
                            "visual_slot_relevance": SCORE_SCHEMA,
                            "visual_evidence": {
                                "type": "string",
                                "minLength": 1,
                            },
                        },
                    },
                }
            },
        },
    )
    instructions = f"""Inspect attached candidate contact sheets in exactly the listed order.
Each image is visibly labeled with its candidate ID.

Resolve the requested focal subject and editorial goal from the maintained request and source
title. For a named real person or fictional character, use visual identity knowledge appropriate
to that source to distinguish the actual subject from other people. Judge every candidate from
sampled pixels. ASR, dialogue implications, slot descriptions, clothing, gender, scene
familiarity, or narrative role are not identity evidence.

A prominent different person must receive required_subject_visibility=1. Before assigning 3 or
higher, at least one sampled frame must contain a sufficiently clear face or person-specific
visual evidence matching the requested identity.

Visibility Likert:
1 = visible person is a different identity;
2 = no usable face comparison, even when a person is prominent;
3 = possible match but unclear, brief, or obscured;
4 = face clearly matches in a meaningful portion;
5 = repeated, unmistakable face match with dominant visibility.

<candidates>
{json.dumps(details.candidates, ensure_ascii=False)}
</candidates>"""
    return PromptPackage(
        stage=PromptStage.PLANNER,
        task=PromptTask.CANDIDATE_VISUAL_SCORING,
        prompt_version="1.0",
        operation=details.operation,
        system_prompt=(
            "You inspect source-video contact sheets for a professional editor. Resolve the "
            "requested subject from the maintained request and source title, then judge whether "
            "that subject and action are actually visible. Identity must come from pixels, never "
            "dialogue or assumptions. Return strict JSON only."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=("request",),
        modality=PromptModality.TEXT_AND_IMAGES,
    )


def _pairwise_scoring(details: PairwiseScoringDetails) -> PromptPackage:
    previous_ids = sorted(
        {
            str(pair["previous_candidate_id"])
            for pair in details.pair_specs
        }
    )
    current_ids = sorted(
        {
            str(pair["current_candidate_id"])
            for pair in details.pair_specs
        }
    )
    pair_count = len(details.pair_specs)
    contract = ResponseContract(
        version="1.0",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "array",
                    "minItems": pair_count,
                    "maxItems": pair_count,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": [
                            "previous_candidate_id",
                            "current_candidate_id",
                            "visual_continuity",
                            "emotional_continuity",
                            "narrative_bridge",
                            "evidence",
                        ],
                        "properties": {
                            "previous_candidate_id": {
                                "type": "string",
                                "enum": previous_ids,
                            },
                            "current_candidate_id": {
                                "type": "string",
                                "enum": current_ids,
                            },
                            "visual_continuity": SCORE_SCHEMA,
                            "emotional_continuity": SCORE_SCHEMA,
                            "narrative_bridge": SCORE_SCHEMA,
                            "evidence": {"type": "string", "minLength": 1},
                        },
                    },
                }
            },
        },
    )
    instructions = f"""Score all {pair_count} candidate combinations across this adjacent Slot
boundary.

The attached images contain TAIL contact sheets for previous candidates followed by HEAD contact
sheets for current candidates. Evaluate only a direct hard cut. Do not propose or rely on fades,
dissolves, generated bridge shots, or transition effects.

Use the maintained request to judge intent. Slot descriptions and candidate text are targets and
context, not proof. Base visual and emotional judgments on pixels.

Scores:
- visual_continuity: composition, location/light/color compatibility, screen direction, body
  position, and whether the direct cut looks intentional;
- emotional_continuity: whether visible affect/action changes coherently or through motivated
  contrast;
- narrative_bridge: whether visible before/after states advance the requested story and supplied
  continuity_from_previous.

<previous_slot>
{json.dumps(details.previous_slot, ensure_ascii=False)}
</previous_slot>
<current_slot>
{json.dumps(details.current_slot, ensure_ascii=False)}
</current_slot>
<candidate_pairs>
{json.dumps(details.pair_specs, ensure_ascii=False)}
</candidate_pairs>"""
    return PromptPackage(
        stage=PromptStage.PLANNER,
        task=PromptTask.PAIRWISE_SCORING,
        prompt_version="1.0",
        operation=details.operation,
        system_prompt=(
            "You evaluate whether two real source-video fragments form a coherent direct hard "
            "cut. Judge the visible tail of the first against the visible head of the second. "
            "Do not assume slot text is visible fact and do not rely on transition effects. "
            "Return strict JSON only."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=("request",),
        modality=PromptModality.TEXT_AND_IMAGES,
    )


def _script_review(details: ScriptReviewDetails) -> PromptPackage:
    slot_ids = [str(slot["slot_id"]) for slot in details.slots]
    patch_schemas = []
    for slot_id in slot_ids:
        candidate_ids = [
            str(candidate["candidate_id"])
            for candidate in details.candidate_pool[slot_id]
        ]
        patch_schemas.append(
            {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "operation",
                    "slot_id",
                    "candidate_id",
                    "reason",
                ],
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["keep", "replace"],
                    },
                    "slot_id": {"type": "string", "const": slot_id},
                    "candidate_id": {
                        "type": "string",
                        "enum": candidate_ids,
                    },
                    "reason": {"type": "string", "minLength": 1},
                },
            }
        )
    contract = ResponseContract(
        version="1.0",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["patches"],
            "properties": {
                "patches": {
                    "type": "array",
                    "maxItems": len(slot_ids),
                    "items": {"oneOf": patch_schemas},
                }
            },
        },
    )
    instructions = """Review the current script as a sequence, focusing on instruction coverage,
music-energy fit, temporal progression, and adjacent-clip continuity.

Return only minimal replacements that clearly improve the full path. Do not change a slot merely
for variety. All output uses direct hard cuts between source fragments. Precomputed visual
continuity scores will reject a patch subset that degrades the weighted full-path score. Use only
candidate IDs supplied in the maintained candidate pool."""
    return PromptPackage(
        stage=PromptStage.PLANNER,
        task=PromptTask.SCRIPT_REVIEW,
        prompt_version="1.0",
        operation="Script patch review",
        system_prompt=(
            "You review a structured edit timeline and return minimal patch operations. Use only "
            "candidate IDs supplied in the maintained context. Return strict JSON only."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=(
            "request",
            "music_profile",
            "video_description",
            "edit_plan",
            "candidate_pool",
            "current_script",
        ),
        modality=PromptModality.TEXT,
        output_artifact="latest_patches",
    )


prompt_registry.register(
    PromptStage.PLANNER,
    PromptTask.SLOT_PLANNING,
    _slot_planning,
)
prompt_registry.register(
    PromptStage.PLANNER,
    PromptTask.CANDIDATE_RETRIEVAL,
    _candidate_retrieval,
)
prompt_registry.register(
    PromptStage.PLANNER,
    PromptTask.CANDIDATE_VISUAL_SCORING,
    _candidate_visual_scoring,
)
prompt_registry.register(
    PromptStage.PLANNER,
    PromptTask.PAIRWISE_SCORING,
    _pairwise_scoring,
)
prompt_registry.register(
    PromptStage.PLANNER,
    PromptTask.SCRIPT_REVIEW,
    _script_review,
)
