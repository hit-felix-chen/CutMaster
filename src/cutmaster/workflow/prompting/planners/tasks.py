from __future__ import annotations

import json
from dataclasses import dataclass
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


SCORE_SCHEMA = {
    "type": "number",
    "minimum": 0.0,
    "maximum": 1.0,
}


@dataclass(frozen=True)
class SlotArrangementDetails:
    target_duration_sec: float
    target_clip_duration_sec: float
    allowed_segment_ids: list[str]
    retry_note: str
    mode: str
    existing_slots: list[dict[str, Any]]
    target_slot_constraints: dict[str, dict[str, Any]]
    rejection_feedback: list[dict[str, Any]]


@dataclass(frozen=True)
class DialogueAnchorSelectionDetails:
    slots: list[dict[str, Any]]
    video_summary: dict[str, Any]
    source_segments: list[dict[str, Any]]
    dialogue_constraints_by_slot: dict[str, dict[str, Any]]
    max_anchors: int
    min_anchor_duration_sec: float


@dataclass(frozen=True)
class CandidateRetrievalDetails:
    operation: str
    candidates_per_slot: int
    slots: list[dict[str, Any]]
    confirmed_candidates: dict[str, list[dict[str, Any]]]
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


def _slot_arrangement(details: SlotArrangementDetails) -> PromptPackage:
    common_required = [
        "narrative_role",
        "content_description",
        "target_emotion",
        "target_emotional_intensity",
        "target_kinetic_energy",
        "desired_duration_sec",
        "continuity_from_previous",
        "source_segment_ids",
        "required_visible_subjects",
    ]

    def slot_schema(
        allowed_segment_ids: list[str],
        *,
        slot_id: str | None = None,
        desired_duration_sec: float | None = None,
        planned_duration_sec: float | None = None,
    ) -> dict[str, Any]:
        required = [*common_required]
        properties: dict[str, Any] = {
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
            "content_description": {"type": "string", "minLength": 1},
            "target_emotion": {"type": "string", "minLength": 1},
            "target_emotional_intensity": SCORE_SCHEMA,
            "target_kinetic_energy": SCORE_SCHEMA,
            "desired_duration_sec": (
                {"type": "number", "const": desired_duration_sec}
                if desired_duration_sec is not None
                else {"type": "number", "minimum": 1.5}
            ),
            "continuity_from_previous": {"type": "string", "minLength": 1},
            "source_segment_ids": {
                "type": "array",
                "minItems": 1,
                "uniqueItems": True,
                "items": {
                    "type": "string",
                    "enum": allowed_segment_ids,
                },
            },
            "required_visible_subjects": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string", "minLength": 1},
            },
        }
        if slot_id is not None:
            required.insert(0, "slot_id")
            properties["slot_id"] = {"type": "string", "const": slot_id}
        if planned_duration_sec is not None:
            required.append("planned_duration_sec")
            properties["planned_duration_sec"] = {
                "type": "number",
                "const": planned_duration_sec,
            }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": required,
            "properties": properties,
        }

    targeted = details.mode == "targeted"
    if details.mode not in {"full", "targeted"}:
        raise ValueError(f"Unknown Slot arrangement mode: {details.mode}")
    if targeted:
        target_schemas = [
            slot_schema(
                list(constraint["allowed_segment_ids"]),
                slot_id=slot_id,
                desired_duration_sec=float(constraint["desired_duration_sec"]),
                planned_duration_sec=float(constraint["planned_duration_sec"]),
            )
            for slot_id, constraint in details.target_slot_constraints.items()
        ]
        slots_schema: dict[str, Any] = {
            "type": "array",
            "minItems": len(target_schemas),
            "maxItems": len(target_schemas),
            "items": {"oneOf": target_schemas},
        }
    else:
        slots_schema = {
            "type": "array",
            "minItems": 1,
            "items": slot_schema(details.allowed_segment_ids),
        }
    contract = ResponseContract(
        version="2.0",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["slots"],
            "properties": {"slots": slots_schema},
        },
    )
    retry_note = f"\n{details.retry_note.strip()}\n" if details.retry_note else ""
    if targeted:
        instructions = f"""Redesign only the specified failed edit Slots. This is a local repair
of an existing plan, not a new timeline. Return exactly one replacement for every Slot in
target_slot_constraints in a single response. Preserve every target slot_id and its exact
desired_duration_sec and planned_duration_sec. The original planned_duration_sec is the
authoritative visual clip duration because it already incorporates beat-aligned boundary
adjustments. Do not return or modify any other Slot.

Use the maintained request, compact music profile, and original structured source story context
from the first Arrangement Architect call. Treat existing_slot_plan as authoritative for all unaffected Slots. Each
replacement must fit chronologically between its previous_fixed_slot and next_fixed_slot and may
use only that Slot's allowed_segment_ids. Multiple replacement Slots must remain in strictly
increasing source order, with every Slot's maximum Segment index strictly lower than the next
Slot's minimum Segment index. Never repeat a source_segment_ids assignment listed in
forbidden_segment_assignments.

The visual candidate diagnostics rejected the earlier candidates for identity, relevance, or
static imagery, or the deterministic duration check proved that no assigned Segment is longer
than one complete planned clip. Use rejection_feedback to
correct the actual cause. Every feedback item has reason_code, diagnosis, and repair_requirement:
reason_code is the stable machine-readable category, diagnosis explains the concrete failed
constraint with measured values, and repair_requirement is mandatory for the replacement. Redesign
the Slot's visible event,
required_visible_subjects, and source_segment_ids so that one continuous
planned_duration_sec-long passage is visually realizable. Do not merely paraphrase the failed
description while retaining unsupported subjects or source evidence. Role, team, and object
subjects do not require a named-person face match; use precise required subjects that the source
descriptions can visibly establish.

Source quality and relevance to the maintained request always take priority. Among comparably
strong assignments inside the allowed chronological intervals, distribute source_segment_ids as
evenly as practical instead of clustering replacement Slots in consecutive or nearby Segments.
Preserve enough source-timeline room for every later Slot; do not push a replacement toward an
interval boundary when an equally strong, more evenly spaced Segment is available.

Do not change output timing or add or remove Slots. Dialogue anchors are not part of this response:
when a redesigned Slot moves away from an anchored source Segment, the application invalidates the
old anchor and runs dialogue-anchor selection again after this repair. Do not preserve a poor
Segment assignment merely because existing_slot_plan shows an anchor there. Do not use title
cards, opening or end credits, production logos, legal cards, or blank frames unless explicitly
required by the maintained request.

<existing_slot_plan>
{json.dumps(details.existing_slots, ensure_ascii=False)}
</existing_slot_plan>
<target_slot_constraints>
{json.dumps(details.target_slot_constraints, ensure_ascii=False)}
</target_slot_constraints>
<rejection_feedback>
{json.dumps(details.rejection_feedback, ensure_ascii=False)}
</rejection_feedback>
{retry_note}"""
    else:
        instructions = f"""Create a sequence of edit slots for the maintained request and structured
video description. The requested output duration is {details.target_duration_sec:.1f} seconds.
Choose the number of slots yourself from the narrative needs, available source material, and the
compact music profile's macro energy sections. There is no predetermined clip count. A Slot represents one
continuous source clip, not an entire narrative chapter. Design the visual rhythm around
target_clip_duration_sec={details.target_clip_duration_sec:.1f}. The average desired_duration_sec
must remain within 12.5% of that target, and no individual Slot may exceed
{details.target_clip_duration_sec * 1.5:.1f} seconds. Assign an initial desired_duration_sec to
every Slot, with all desired durations totaling the requested output duration within 0.5 seconds.
Do not pre-snap durations to beats; a later deterministic audio-alignment stage will make small
boundary adjustments.

Dialogue length never justifies making a visual Slot longer. Original dialogue is selected in a
later stage on an independent audio timeline. A long line or continuous exchange starts on its
corresponding original-picture passage, then continues across other short visual Slots as a
start-aligned L-cut. Plan enough short visual Slots for the picture to keep cutting while such
dialogue plays.

Each slot must be realizable from supplied source_segment_ids. Use the reusable story summary for
plot understanding, and use Segment summaries and appearing characters as visual source truth.
At least one assigned source Segment must be longer than that Slot's desired_duration_sec so a
complete candidate passage and a small timestamp displacement are both possible. Multiple
alternative candidates may overlap; do not reserve several non-overlapping windows per Slot.
Never invent props,
gestures, settings, identities, or actions absent from the description. Keep source_segment_ids
in strictly increasing source order across Slots: every Slot's maximum Segment index must be
strictly lower than the next Slot's minimum Segment index. Never reuse one Segment in two Slots.

Source quality, request relevance, and narrative value always take priority over spacing. Among
comparably strong Segment assignments, distribute source_segment_ids as evenly as practical
across the usable source timeline instead of clustering Slots in consecutive or nearby Segments.
Plan this distribution globally: reserve sufficient chronological Segment space for all remaining
Slots, especially near the end of the timeline.

The narrative_role values are reusable labels, not a mandatory five-act template. Every role may
appear multiple times or not appear at all; do not create one Slot per enum value. For a
character-focused request, list the focal character in required_visible_subjects whenever that
character must be seen. Maintain a coherent progression. Every slot after the first must explain
how it continues or contrasts with the previous slot. Do not use title cards, opening or end
credits, production logos, legal cards, or blank frames unless the maintained request explicitly
requires them.
{retry_note}"""
    return PromptPackage(
        stage=PromptStage.PLANNERS,
        task=PromptTask.SLOT_ARRANGEMENT,
        prompt_version="3.4",
        operation=(
            "Arrangement Architect targeted repair"
            if targeted
            else "Arrangement Architect Slot design"
        ),
        system_prompt=(
            "You are CutMaster's Arrangement Architect. Arrange the output timeline, pacing, "
            "emotional progression, and narrative structure, but do not select source "
            "timestamps. Return strict JSON only."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=(
            "request",
            "music_profile",
            "source_story_context",
            "planners_feedback",
        ),
        modality=PromptModality.TEXT,
        output_artifact=(
            "targeted_slot_redesign"
            if targeted
            else "edit_plan_unaligned"
        ),
    )


def _candidate_item_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "timestamp",
            "description",
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
            "description": {"type": "string", "minLength": 1},
            "semantic_relevance": SCORE_SCHEMA,
            "emotional_intensity": SCORE_SCHEMA,
            "salience": SCORE_SCHEMA,
        },
    }


def _dialogue_anchor_selection(
    details: DialogueAnchorSelectionDetails,
) -> PromptPackage:
    dialogue_ids_by_segment = {
        str(segment["segment_id"]): [
            str(item["dialogue_id"])
            for item in segment["dialogue_items"]
        ]
        for segment in details.source_segments
    }
    anchor_schemas: list[dict[str, Any]] = []
    for slot in details.slots:
        slot_id = str(slot["slot_id"])
        constraint = details.dialogue_constraints_by_slot.get(slot_id) or {}
        allowed_segment_ids = [
            str(value)
            for value in constraint.get("allowed_segment_ids") or []
            if dialogue_ids_by_segment.get(str(value))
        ]
        if not allowed_segment_ids:
            continue
        allowed_dialogue_ids = list(
            dict.fromkeys(
                dialogue_id
                for segment_id in allowed_segment_ids
                for dialogue_id in dialogue_ids_by_segment[segment_id]
            )
        )
        anchor_schemas.append(
            {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "slot_id",
                    "source_segment_id",
                    "first_dialogue_id",
                    "last_dialogue_id",
                    "narrative_significance",
                    "request_relevance",
                    "standalone_meaning",
                    "importance_likert",
                    "coherence_likert",
                ],
                "properties": {
                    "slot_id": {
                        "type": "string",
                        "const": slot_id,
                    },
                    "source_segment_id": {
                        "type": "string",
                        "enum": allowed_segment_ids,
                    },
                    "first_dialogue_id": {
                        "type": "string",
                        "enum": allowed_dialogue_ids,
                    },
                    "last_dialogue_id": {
                        "type": "string",
                        "enum": allowed_dialogue_ids,
                    },
                    "narrative_significance": {
                        "type": "string",
                        "minLength": 1,
                    },
                    "request_relevance": {
                        "type": "string",
                        "minLength": 1,
                    },
                    "standalone_meaning": {
                        "type": "string",
                        "minLength": 1,
                    },
                    "importance_likert": {
                        "type": "integer",
                        "enum": [4, 5],
                    },
                    "coherence_likert": {
                        "type": "integer",
                        "enum": [4, 5],
                    },
                },
            }
        )
    minimum = 1 if anchor_schemas else 0
    contract = ResponseContract(
        version="4.0",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["anchors"],
            "properties": {
                "anchors": {
                    "type": "array",
                    "minItems": minimum,
                    "maxItems": min(
                        details.max_anchors,
                        len(anchor_schemas),
                    ),
                    "uniqueItems": True,
                    "items": (
                        {"oneOf": anchor_schemas}
                        if anchor_schemas
                        else {"type": "object"}
                    ),
                }
            },
        },
    )
    instructions = f"""Select a small set of original-dialogue anchors for the maintained user
request after the visual Slot timeline has been created.

Start from the user request and reusable video_summary. Identify the few moments whose original
speech most clearly communicates the requested character arc, conflict, decision, revelation, or
theme. Select at most {details.max_anchors} anchors. Every selected spoken range must last at least
{details.min_anchor_duration_sec:.1f} seconds and must remain meaningful when heard in the final
short edit without unexplained surrounding dialogue.

For each anchor, choose one source_segment_id allowed by that Slot, followed by an inclusive
first_dialogue_id and last_dialogue_id from that same Segment. The application expands the
inclusive endpoints to every intervening dialogue item and validates continuity, source order,
minimum and maximum duration, source-picture bounds, output-timeline fit, and overlap. Never skip
an intervening dialogue item, cross a Segment boundary, or combine nonconsecutive passages.

Use dialogue_constraints_by_slot for each Slot's allowed Segments and exact duration limits.
Do not select greetings, acknowledgements, exclamations, sentence fragments, generic reactions,
or isolated replies such as “yes”, “no”, “good”, or “oh”. Do not pad a range with unrelated
neighboring speech merely to satisfy duration.

Every selected anchor uses the same L-cut layout. Its original speech starts exactly at the
selected Slot's output start. The selected Slot starts with the corresponding original picture
and sound; if the speech is longer than that Slot, the sound continues across subsequent visual
Slots. The corresponding portion of original picture and original sound must retain the same
source-time mapping wherever they coexist. Never enlarge or merge visual Slots to contain speech.
The complete audio range must stay inside the output timeline and must not overlap another anchor.

Never skip an intervening dialogue item, cross a Segment boundary, join unrelated exchanges,
reorder dialogue, or manufacture words. Prefer a small set of memorable anchors distributed
across the requested narrative, never more than one per Slot, and preserve non-overlapping source
chronology across selected anchors. It is valid to leave most Slots without original speech.

For every selection, explain its narrative_significance, direct request_relevance, and
standalone_meaning. Give importance_likert and coherence_likert only as 4 or 5; omit the anchor
entirely if either quality would be lower.

Use each Slot's selected_source_segments for the Segment's plot position, description, emotional
meaning, characters, and exact selectable dialogue. Avoid voice-over, off-screen speech, credits,
title cards, and speech over unrelated imagery.

<slots>
{json.dumps(details.slots, ensure_ascii=False)}
</slots>

<video_summary>
{json.dumps(details.video_summary, ensure_ascii=False)}
</video_summary>

<selected_source_segments>
{json.dumps(details.source_segments, ensure_ascii=False)}
</selected_source_segments>

<dialogue_constraints_by_slot>
{json.dumps(details.dialogue_constraints_by_slot, ensure_ascii=False)}
</dialogue_constraints_by_slot>"""
    return PromptPackage(
        stage=PromptStage.PLANNERS,
        task=PromptTask.DIALOGUE_ANCHOR_SELECTION,
        prompt_version="4.1",
        operation="Story Editor original-dialogue anchoring",
        system_prompt=(
            "You are CutMaster's Story Editor. Select a few meaningful, coherent original-speech "
            "passages that directly anchor the user's requested story. Each passage may be one "
            "complete line or multiple consecutive lines. Every passage starts with its "
            "corresponding source picture as a start-aligned L-cut. Return strict JSON only."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=("request",),
        modality=PromptModality.TEXT,
        output_artifact="dialogue_anchor_selection",
    )


def _candidate_retrieval(details: CandidateRetrievalDetails) -> PromptPackage:
    slot_group_schemas = []
    for slot in details.slots:
        slot_id = str(slot["slot_id"])
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
                        "items": _candidate_item_schema(),
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
    instructions = f"""Retrieve exactly {details.candidates_per_slot} source candidates
for every supplied edit Slot.

Use only supplied structured source Segments and their Shot-level visual annotations. Select each
candidate as a precise time window:
- its duration must equal that Slot's planned_duration_sec, to millisecond timestamp precision;
- it must be fully contained in the supplied Segment timeline;
- it may start or end inside a Shot and does not need to use Shot boundaries;
- candidates for the same Slot may overlap each other and confirmed candidates, allowing small
  timestamp displacements, but must not exactly duplicate another candidate or excluded range.
Silent Segments are valid source material. Do not return source Shot IDs; the application derives
the overlapping Shots deterministically from the validated timestamp.

Prefer each Slot's source_segment_ids, preserve source chronology, and avoid exact excluded
timestamps. Describe only the content expected inside the selected time window, based on its
overlapping Shot descriptions. Use the maintained video summary for plot understanding; exact
transcript text is intentionally omitted from visual candidate retrieval. Never let inferred
speech override visible identity or action. Score semantic relevance, emotional intensity, and
editorial salience from 0 to 1.

Candidates in confirmed_candidates already passed timestamp validation and VLM visual grounding.
They are permanently retained. Return only the candidates requested by this contract and never
duplicate a confirmed or excluded timestamp exactly. Excluded ranges also contain rejected
windows; a genuinely different, slightly displaced window may overlap them.

<slots>
{json.dumps(details.slots, ensure_ascii=False)}
</slots>
<confirmed_candidates>
{json.dumps(details.confirmed_candidates, ensure_ascii=False)}
</confirmed_candidates>
<excluded_ranges>
{json.dumps(details.excluded_ranges, ensure_ascii=False)}
</excluded_ranges>
<available_source_segments_by_slot>
{json.dumps(details.source_segments_by_slot, ensure_ascii=False)}
</available_source_segments_by_slot>"""
    return PromptPackage(
        stage=PromptStage.PLANNERS,
        task=PromptTask.CANDIDATE_RETRIEVAL,
        prompt_version="2.1",
        operation=details.operation,
        system_prompt=(
            "You are CutMaster's Timeline Scout. Scout real source-video passages from a "
            "structured VideoDescription whose Segment timeline and Shot annotations are "
            "authoritative. Choose precise fixed-duration windows within that timeline; never "
            "invent timestamps, visuals, or dialogue. Return strict JSON only."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=("request", "video_summary"),
        modality=PromptModality.TEXT,
    )


def _candidate_visual_scoring(
    details: CandidateVisualScoringDetails,
) -> PromptPackage:
    candidate_ids = [
        str(candidate["candidate_id"]) for candidate in details.candidates
    ]
    contract = ResponseContract(
        version="2.1",
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
                            "visual_slot_relevance": {
                                "type": "integer",
                                "enum": [1, 2, 3, 4, 5],
                            },
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
to that source to distinguish the actual subject from other people.

Use the attached sampled pixels as primary evidence. Each candidate also includes its source
Segment video description and the Shot descriptions overlapping the exact candidate window.
Use those structured visual annotations as supporting evidence for visible identity, team or
group membership, objects, and actions. In particular, character identity_evidence, readable
jersey names or numbers, and Shot visual_evidence may corroborate a sampled frame.

The Segment dialogue_items and candidate_dialogue contain ASR dialogue associated with the
source. Dialogue may provide supporting evidence about the named speaker, player, action, or
event when its timestamp overlaps the candidate and agrees with the visual evidence. It is not,
by itself, proof that a mentioned person is visible: commentary may describe off-screen action,
earlier events, or another camera view. Do not use the intended Slot description, generic
clothing, gender, scene familiarity, or narrative role as identity evidence. Never let dialogue,
a Segment summary, or a Shot description override contradictory sampled pixels, and do not
transfer identity evidence from a non-overlapping Shot.

A prominent confirmed different person must receive required_subject_visibility=1. Before
assigning 3 or higher, the combined sampled frames, overlapping Shot visual annotations, and
time-aligned dialogue must provide a plausible match to the requested identity; dialogue alone
is insufficient.

Visibility Likert:
1 = visible person is a different identity;
2 = no usable face comparison, even when a person is prominent;
3 = possible match but unclear, brief, or obscured;
4 = face clearly matches in a meaningful portion;
5 = repeated, unmistakable face match with dominant visibility.

Visual Slot Relevance Likert:
1 = visible content conflicts with or is unrelated to the intended Slot content;
2 = little usable visual evidence supports the intended content;
3 = partial or ambiguous visual match;
4 = clear visual match in a meaningful portion;
5 = repeated, dominant visual evidence strongly matches the intended content.

<candidates>
{json.dumps(details.candidates, ensure_ascii=False)}
</candidates>"""
    return PromptPackage(
        stage=PromptStage.PLANNERS,
        task=PromptTask.CANDIDATE_VISUAL_SCORING,
        prompt_version="2.0",
        operation=details.operation,
        system_prompt=(
            "As CutMaster's Timeline Scout, inspect source-video contact sheets and resolve the "
            "requested subject from the maintained request and source title, then judge whether "
            "that subject and action are actually visible. Identity must come from pixels, "
            "never dialogue or assumptions. Return strict JSON only."
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
        stage=PromptStage.PLANNERS,
        task=PromptTask.PAIRWISE_SCORING,
        prompt_version="1.0",
        operation=details.operation,
        system_prompt=(
            "As CutMaster's Edit Composer, evaluate whether two real source-video fragments form "
            "a coherent direct hard cut. Judge the visible tail of the first against the visible "
            "head of the second. Do not assume Slot text is visible fact and do not rely on "
            "transition effects. Return strict JSON only."
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
        stage=PromptStage.PLANNERS,
        task=PromptTask.SCRIPT_REVIEW,
        prompt_version="1.0",
        operation="Revision Editor script review",
        system_prompt=(
            "You are CutMaster's Revision Editor. Review the composed edit and return minimal "
            "patch operations. Use only candidate IDs supplied in the maintained context. "
            "Return strict JSON only."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=(
            "request",
            "music_profile",
            "video_summary",
            "edit_plan",
            "candidate_pool",
            "current_script",
        ),
        modality=PromptModality.TEXT,
        output_artifact="latest_patches",
    )


prompt_registry.register(
    PromptStage.PLANNERS,
    PromptTask.SLOT_ARRANGEMENT,
    _slot_arrangement,
)
prompt_registry.register(
    PromptStage.PLANNERS,
    PromptTask.DIALOGUE_ANCHOR_SELECTION,
    _dialogue_anchor_selection,
)
prompt_registry.register(
    PromptStage.PLANNERS,
    PromptTask.CANDIDATE_RETRIEVAL,
    _candidate_retrieval,
)
prompt_registry.register(
    PromptStage.PLANNERS,
    PromptTask.CANDIDATE_VISUAL_SCORING,
    _candidate_visual_scoring,
)
prompt_registry.register(
    PromptStage.PLANNERS,
    PromptTask.PAIRWISE_SCORING,
    _pairwise_scoring,
)
prompt_registry.register(
    PromptStage.PLANNERS,
    PromptTask.SCRIPT_REVIEW,
    _script_review,
)
