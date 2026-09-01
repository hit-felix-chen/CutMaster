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
    min_anchors: int = 1
    preserved_anchors: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class CandidateRetrievalDetails:
    operation: str
    round_index: int
    round_phase: str
    trajectories_per_group: int
    group: dict[str, Any]
    slots: list[dict[str, Any]]
    confirmed_trajectories: list[dict[str, Any]]
    excluded_ranges_by_slot: dict[str, list[str]]
    rejected_trajectory_signatures: list[str]
    rejection_feedback: list[dict[str, Any]]
    planning_segment: dict[str, Any]


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
        "source_segment_id",
        "required_visible_subjects",
    ]

    def slot_schema(
        allowed_segment_ids: list[str],
        *,
        slot_id: str | None = None,
        desired_duration_sec: float | None = None,
        planned_duration_ms: int | None = None,
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
            "source_segment_id": {
                "type": "string",
                "enum": allowed_segment_ids,
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
        if planned_duration_ms is not None:
            required.append("planned_duration_ms")
            properties["planned_duration_ms"] = {
                "type": "integer",
                "const": planned_duration_ms,
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
                planned_duration_ms=int(constraint["planned_duration_ms"]),
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
        instructions = f"""Repair the specified Slot Groups inside one or more independent local
windows of an existing plan. This is not a new timeline. target_slot_constraints contains every
Slot in each failed group and includes its complete adjacent groups only when the failed group
cannot move to another legal Segment by itself. Return exactly one replacement for every listed
Slot in a single response. Do not return or modify any unlisted Slot.

Preserve every target slot_id and its exact desired_duration_sec and planned_duration_ms. The
planned_duration_ms values are authoritative because they already incorporate beat-aligned
boundary adjustments. Never add, remove, reorder, split, or merge Slots.

Use the maintained request, compact music profile, and original structured source story context
from the first Arrangement Architect call. Treat existing_slot_plan as authoritative for all
unaffected Slots. Redesign all members of an original_group_id together. Every member of that
group must choose the same source_segment_id, and each replacement may use only its own
allowed_segment_ids. Distinct original groups must use different Segments, with Segment identifiers
strictly increasing in Slot order. Never merge groups, return to an earlier Segment, or reuse a
Segment after the plan has moved forward.

For every Segment selected in a repair window, add the planned_duration_ms of all returned Slots
in its original group. That duration must not exceed the Segment's complete source duration. This
capacity check is for the whole group, not for one Slot in isolation. Leave enough ordered,
non-overlapping source time for one complete trajectory containing every group Slot.

The visual candidate diagnostics rejected the earlier candidates for identity, relevance, or
static imagery, no complete group trajectory survived validation, or the assigned Segment lacked
capacity for the whole group. Use rejection_feedback to correct the actual cause. Every feedback
item has reason_code, diagnosis, and repair_requirement: reason_code is the stable machine-readable
category, diagnosis explains the concrete failed constraint with measured values, and
repair_requirement is mandatory. Redesign the group's visible events, required_visible_subjects,
and shared source_segment_id together so that a complete ordered trajectory is visually
realizable. Do not merely paraphrase the failed descriptions while retaining unsupported subjects
or source evidence. Role, team, and object subjects do not require a named-person face match; use
precise required subjects that the source descriptions can visibly establish.

Source quality and relevance to the maintained request always take priority. Preserve enough
chronological room between the fixed groups immediately before and after each repair window. When
an adjacent group was included only to open a feasible assignment, change no more of its visible
intent than necessary.

Dialogue anchors are not part of this response. After applying all listed repair windows, the
application discards the old Anchor split and runs Story Editor again. Do not preserve a poor
Segment assignment merely because existing_slot_plan shows an Anchor there. Do not use title cards,
opening or end credits, production logos, legal cards, or blank frames unless explicitly required
by the maintained request.

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

Each Slot must be realizable from its supplied source_segment_id. Use the reusable story summary for
plot understanding, and use Segment summaries and appearing characters as visual source truth.
Never invent props, gestures, settings, identities, or actions absent from the description.
Adjacent Slots may share one Segment and will then form one Slot Group. Every Slot in that group
must use the same source_segment_id, and their total desired duration must fit inside that Segment.
After the plan moves to a later Segment it may not return to an earlier or already-used Segment.
Thus Slot Segment identifiers are monotonically non-decreasing, while Slot Group Segment
identifiers are strictly increasing.

Source quality, request relevance, and narrative value always take priority over spacing. Among
comparably strong Segment assignments, distribute source_segment_id values as evenly as practical
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
        prompt_version="4.2" if targeted else "4.0",
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
            ("request", "music_profile", "source_story_context")
            if targeted
            else (
                "request",
                "music_profile",
                "source_story_context",
                "planners_feedback",
            )
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
            "source_start_ms",
            "description",
            "semantic_relevance",
            "emotional_intensity",
            "salience",
        ],
        "properties": {
            "source_start_ms": {"type": "integer"},
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
        output_audio_start_sec = constraint.get("output_audio_start_sec")
        if not isinstance(output_audio_start_sec, (int, float)):
            raise ValueError(
                f"Dialogue constraint for {slot_id} must include its "
                "output_audio_start_sec"
            )
        allowed_by_segment = constraint.get(
            "allowed_last_dialogue_ids_by_segment_and_first"
        ) or {}
        output_ends_by_segment = constraint.get(
            "output_audio_end_sec_by_segment_and_first_and_last"
        ) or {}
        endpoint_groups: list[dict[str, Any]] = []
        for raw_segment_id, allowed_by_first in allowed_by_segment.items():
            segment_id = str(raw_segment_id)
            segment_dialogue_ids = dialogue_ids_by_segment.get(segment_id) or []
            output_ends_by_first = output_ends_by_segment.get(segment_id) or {}
            for raw_first_id, raw_last_ids in allowed_by_first.items():
                first_dialogue_id = str(raw_first_id)
                if first_dialogue_id not in segment_dialogue_ids:
                    continue
                output_ends_by_last = (
                    output_ends_by_first.get(first_dialogue_id) or {}
                )
                last_dialogue_ids: list[str] = []
                for raw_last_id in raw_last_ids:
                    last_dialogue_id = str(raw_last_id)
                    if last_dialogue_id not in segment_dialogue_ids:
                        continue
                    output_audio_end_sec = output_ends_by_last.get(
                        last_dialogue_id
                    )
                    if not isinstance(output_audio_end_sec, (int, float)):
                        raise ValueError(
                            f"Allowed dialogue endpoint {slot_id}/"
                            f"{segment_id}/{first_dialogue_id}/"
                            f"{last_dialogue_id} must include its "
                            "output_audio_end_sec"
                        )
                    if float(output_audio_end_sec) <= float(
                        output_audio_start_sec
                    ):
                        raise ValueError(
                            f"Allowed dialogue endpoint {slot_id}/"
                            f"{segment_id}/{first_dialogue_id}/"
                            f"{last_dialogue_id} has an invalid output audio range"
                        )
                    last_dialogue_ids.append(last_dialogue_id)
                if last_dialogue_ids:
                    endpoint_groups.append(
                        {
                            "source_segment_id": segment_id,
                            "first_dialogue_id": first_dialogue_id,
                            "last_dialogue_ids": last_dialogue_ids,
                        }
                    )
        if not endpoint_groups:
            continue
        allowed_segment_ids = list(
            dict.fromkeys(
                item["source_segment_id"] for item in endpoint_groups
            )
        )
        allowed_first_dialogue_ids = list(
            dict.fromkeys(
                item["first_dialogue_id"] for item in endpoint_groups
            )
        )
        allowed_last_dialogue_ids = list(
            dict.fromkeys(
                last_dialogue_id
                for item in endpoint_groups
                for last_dialogue_id in item["last_dialogue_ids"]
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
                        "enum": allowed_first_dialogue_ids,
                    },
                    "last_dialogue_id": {
                        "type": "string",
                        "enum": allowed_last_dialogue_ids,
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
    contract = ResponseContract(
        version="4.3",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["anchors"],
            "properties": {
                "anchors": {
                    "type": "array",
                    "minItems": details.min_anchors,
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
    minimum_instruction = (
        "Return at least one strong Anchor."
        if details.min_anchors
        else (
            "It is valid to return no new Anchor because unchanged groups already "
            "retain a valid Anchor."
        )
    )
    prompt_constraints = {
        slot_id: {
            key: value
            for key, value in constraint.items()
            if key
            not in {
                "allowed_last_dialogue_ids_by_segment_and_first",
                "allowed_passages",
            }
        }
        for slot_id, constraint in details.dialogue_constraints_by_slot.items()
    }
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

Use each Slot's output_audio_end_sec_by_segment_and_first_and_last map. Its nested Segment, first
ID, and last ID keys are the complete legal endpoint pairs. Select all three keys from one path in
that map. The Slot's output_audio_start_sec is the authoritative audio start, and the value at the
selected nested path is the authoritative audio end. Do not recalculate or ignore that output
range. Each endpoint pair already satisfies the Slot's individual Segment, duration,
picture-bound, and output-timeline limits. The application validates the complete multi-Anchor
combination, including source order, dialogue reuse, audio overlap, and picture-partition capacity.

Each Slot's preferred_anchor_picture_range is soft guidance, not a hard constraint. It is computed
from that Slot's position among the Slots sharing its source Segment. When passages have comparable
narrative quality, prefer one whose complete fixed picture window—from the first dialogue start for
planned_picture_duration_sec—lies inside this range. If no strong legal passage fits the preferred
range, select a legal passage outside it instead; do not omit a needed Anchor, pad its dialogue, or
change its endpoints merely to satisfy the guidance. For multiple Anchors in one Segment, apply
each Slot's own preferred range independently. Extending L-cut audio does not change this picture
range.

Do not select greetings, acknowledgements, exclamations, sentence fragments, generic reactions,
or isolated replies such as “yes”, “no”, “good”, or “oh”. Do not pad a range with unrelated
neighboring speech merely to satisfy duration.

Every selected anchor uses the same L-cut layout. Its original speech starts exactly at the
selected Slot's output start. The selected Slot starts with the corresponding original picture
and sound; if the speech is longer than that Slot, the sound continues across subsequent visual
Slots. The corresponding portion of original picture and original sound must retain the same
source-time mapping wherever they coexist. Never enlarge or merge visual Slots to contain speech.
The complete audio range must stay inside the output timeline and must not overlap another anchor.
Treat output ranges as half-open intervals: two selections conflict whenever one starts before the
other ends. The preserved Anchors below are immutable; every new Anchor must avoid all of their
output ranges as well as every other new Anchor's output range.

Never skip an intervening dialogue item, cross a Segment boundary, join unrelated exchanges,
reorder dialogue, or manufacture words. Prefer a small set of memorable anchors distributed
across the requested narrative, never more than one per Slot, and preserve non-overlapping source
chronology across selected anchors. It is valid to leave most Slots without original speech.
{minimum_instruction}
If prior ASTER feedback reports an Anchor timing, overlap, or partition-capacity failure, correct
that exact failure and choose a different passage or Slot.

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
{json.dumps(prompt_constraints, ensure_ascii=False)}
</dialogue_constraints_by_slot>

<preserved_anchors>
{json.dumps(list(details.preserved_anchors), ensure_ascii=False)}
</preserved_anchors>"""
    return PromptPackage(
        stage=PromptStage.PLANNERS,
        task=PromptTask.DIALOGUE_ANCHOR_SELECTION,
        prompt_version="4.7",
        operation="Story Editor original-dialogue anchoring",
        system_prompt=(
            "You are CutMaster's Story Editor. Select a few meaningful, coherent original-speech "
            "passages that directly anchor the user's requested story. Each passage may be one "
            "complete line or multiple consecutive lines. Every passage starts with its "
            "corresponding source picture as a start-aligned L-cut. Return strict JSON only."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=("request", "planners_feedback"),
        modality=PromptModality.TEXT,
        output_artifact="dialogue_anchor_selection",
    )


def _candidate_retrieval(details: CandidateRetrievalDetails) -> PromptPackage:
    item_schemas: list[dict[str, Any]] = []
    for slot in details.slots:
        slot_id = str(slot["slot_id"])
        schema = _candidate_item_schema()
        schema["required"] = ["slot_id", *schema["required"]]
        schema["properties"] = {
            "slot_id": {"type": "string", "const": slot_id},
            **schema["properties"],
        }
        item_schemas.append(schema)
    trajectory_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "minItems": len(item_schemas),
                "maxItems": len(item_schemas),
                "items": {"oneOf": item_schemas},
            }
        },
    }
    contract = ResponseContract(
        version="2.1",
        schema={
            "type": "object",
            "additionalProperties": False,
            "required": ["trajectories"],
            "properties": {
                "trajectories": {
                    "type": "array",
                    "minItems": details.trajectories_per_group,
                    "maxItems": details.trajectories_per_group,
                    "uniqueItems": True,
                    "items": trajectory_schema,
                }
            },
        },
    )
    instructions = f"""This is Candidate retrieval round {details.round_index} in the
{details.round_phase} phase. Retrieve exactly {details.trajectories_per_group} complete candidate
trajectories for the supplied Slot Group. A trajectory is one indivisible choice: it must contain
exactly one item for every supplied Slot, in the same Slot order. All item windows must lie inside
the one supplied Planning Segment, follow source time, and never overlap. Equality between one
item's end and the next item's start is allowed. Return only source_start_ms as an integer number
of milliseconds for each item; do not return a timestamp range or an end time. The application
derives each end as source_start_ms + planned_duration_ms. Choose every start so the derived whole
trajectory stays inside the Planning Segment, ordered, and non-overlapping.

Use only the supplied structured source Segment and its Shot-level visual annotations. A window
may start or end inside a Shot. Do not return Shot IDs; the application derives them from the
derived time windows. Describe only visible content supported by overlapping Shot annotations.
Use the maintained video summary only for plot context. Never invent visuals, identity, action,
dialogue, or source start times.

Confirmed trajectories already passed all checks and are retained. Return new whole trajectories;
do not duplicate a confirmed trajectory, any rejected trajectory signature, or an excluded Slot
range exactly. Different trajectories may reuse or overlap source time, but every trajectory must
be internally ordered and non-overlapping. Score each item from 0 to 1 for semantic relevance,
emotional intensity, and salience.

rejection_feedback contains every earlier failed request or rejected trajectory for this group.
Each item includes a diagnosis and a preset repair requirement. Correct all of them. During the
final supplement phase, prefer one conservative complete path that passes every hard constraint
over a more ambitious but uncertain visual choice.

<slot_group>
{json.dumps(details.group, ensure_ascii=False)}
</slot_group>
<slots>
{json.dumps(details.slots, ensure_ascii=False)}
</slots>
<planning_segment>
{json.dumps(details.planning_segment, ensure_ascii=False)}
</planning_segment>
<confirmed_trajectories>
{json.dumps(details.confirmed_trajectories, ensure_ascii=False)}
</confirmed_trajectories>
<excluded_ranges_by_slot>
{json.dumps(details.excluded_ranges_by_slot, ensure_ascii=False)}
</excluded_ranges_by_slot>
<rejected_trajectory_signatures>
{json.dumps(details.rejected_trajectory_signatures, ensure_ascii=False)}
</rejected_trajectory_signatures>
<rejection_feedback>
{json.dumps(details.rejection_feedback, ensure_ascii=False)}
</rejection_feedback>"""
    return PromptPackage(
        stage=PromptStage.PLANNERS,
        task=PromptTask.CANDIDATE_RETRIEVAL,
        prompt_version="3.2",
        operation=details.operation,
        system_prompt=(
            "You are CutMaster's Timeline Scout. Scout real source-video passages from a "
            "structured VideoDescription whose Segment timeline and Shot annotations are "
            "authoritative. Return complete, ordered, non-overlapping Slot Group trajectories "
            "inside one Planning Segment. Return source_start_ms only for time placement. "
            "Never invent source times or visuals. Return strict JSON only."
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

Judge each candidate only against that candidate's intended_visible_content and
required_visible_subjects. Do not infer or enforce the global user request, overall edit theme,
focal protagonist, or requirements from any other Slot. A person, group, object, or action from
the global request must not affect this decision unless it is explicitly present in this
candidate's Slot requirements.

Use the attached sampled pixels as primary evidence. Each candidate also includes its source
Segment video description and the Shot descriptions overlapping the exact candidate window.
Use those structured visual annotations as supporting evidence for visible identity, team or
group membership, objects, and actions required by this Slot. These source annotations are local
evidence, not additional requirements. In particular, character identity_evidence, readable
jersey names or numbers, and Shot visual_evidence may corroborate a sampled frame.

The Segment dialogue_items and candidate_dialogue contain ASR dialogue associated with the
source. Dialogue may provide supporting evidence about the named speaker, player, action, or
event when its timestamp overlaps the candidate and agrees with the visual evidence. It is not,
by itself, proof that a mentioned person is visible: commentary may describe off-screen action,
earlier events, or another camera view. The intended Slot description states what to check; it is
not evidence that the content is present. Generic clothing, gender, scene familiarity, or
narrative role are not identity evidence. Never let dialogue, a Segment summary, or a Shot
description override contradictory sampled pixels, and do not transfer identity evidence from a
non-overlapping Shot.

required_subject_visibility evaluates all subjects in required_visible_subjects for this one
candidate. Use the weakest required subject when choosing the score. If the list is empty, return
5 because this check is not applicable. For a named person or fictional character, identity must
be supported by the local evidence above. For a team, group, or object, judge whether that exact
visual requirement is confirmed. Dialogue alone is insufficient.

Required Subject Visibility Likert:
1 = at least one required subject is visibly absent, contradicted, or a different identity;
2 = at least one required subject cannot be visually verified from usable evidence;
3 = every required subject is plausibly present, but at least one is unclear, brief, or obscured;
4 = all required subjects are clearly confirmed in a meaningful portion;
5 = all required subjects are repeatedly or unmistakably confirmed, or the requirement list is empty.

Visual Slot Relevance Likert:
1 = visible content conflicts with or is unrelated to intended_visible_content;
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
        prompt_version="2.1",
        operation=details.operation,
        system_prompt=(
            "As CutMaster's Timeline Scout, inspect source-video contact sheets and judge only "
            "whether each candidate satisfies its own Slot content and required visual subjects. "
            "Do not apply the global request or requirements from other Slots. Visual evidence "
            "must come from the candidate pixels and its local source annotations, never from "
            "assumptions. Return strict JSON only."
        ),
        user_prompt=assemble_user_prompt(instructions, contract),
        response_contract=contract,
        context_keys=(),
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
    group_ids = [str(group_id) for group_id in details.candidate_pool]
    patch_schemas: list[dict[str, Any]] = []
    for group_id in group_ids:
        trajectory_ids = [
            str(trajectory["trajectory_id"])
            for trajectory in details.candidate_pool[group_id]
        ]
        patch_schemas.append(
            {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "operation",
                    "group_id",
                    "trajectory_id",
                    "reason",
                ],
                "properties": {
                    "operation": {
                        "type": "string",
                        "enum": ["keep", "replace"],
                    },
                    "group_id": {"type": "string", "const": group_id},
                    "trajectory_id": {
                        "type": "string",
                        "enum": trajectory_ids,
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
                    "maxItems": len(group_ids),
                    "items": (
                        {"oneOf": patch_schemas}
                        if patch_schemas
                        else {"type": "object"}
                    ),
                }
            },
        },
    )
    instructions = """Review the current script as a sequence, focusing on instruction coverage,
music-energy fit, temporal progression, and adjacent-clip continuity.

Return only minimal Slot Group replacements that clearly improve the full edit. A replacement
must select one complete trajectory from that group's candidate pool. Never replace one Slot or
combine items from different trajectories. Do not change a group merely for variety. All output
uses direct hard cuts. Use only trajectory IDs supplied in the maintained candidate pool."""
    return PromptPackage(
        stage=PromptStage.PLANNERS,
        task=PromptTask.SCRIPT_REVIEW,
        prompt_version="2.0",
        operation="Revision Editor script review",
        system_prompt=(
            "You are CutMaster's Revision Editor. Review the composed edit and return minimal "
            "whole-trajectory patch operations. Use only trajectory IDs supplied in the maintained context. "
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
