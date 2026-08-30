from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cutmaster.configuration.schema import AppConfig, LLMConfig
from cutmaster.workflow.contracts.planners import PlannersRequest
from cutmaster.workflow.planners.tools.music_analysis import (
    build_music_profile,
    compact_music_profile,
    write_music_profile,
)
from cutmaster.workflow.planners.tools.errors import (
    TargetedRepairUnrepairableError,
)
from cutmaster.workflow.planners.tools.planners_feedback import (
    is_hard_failed_slot_record,
)
from cutmaster.workflow.prompting import PromptStage, PromptTask, prompt_registry
from cutmaster.workflow.prompting.failure_catalog import PromptFailureCode
from cutmaster.workflow.prompting.planners import SlotArrangementDetails
from cutmaster.infrastructure.observability.logging import log_event
from cutmaster.workflow.shared.execution_context import WorkflowContext

MIN_SLOT_DURATION_SEC = 1.5
MAX_DURATION_TOTAL_ERROR_SEC = 0.5
MAX_MUSIC_BOUNDARY_SHIFT_SEC = 0.75
MAX_TARGET_DURATION_RATIO = 1.5
MAX_AVERAGE_TARGET_ERROR_RATIO = 0.125
DURATION_TOLERANCE_SEC = 1e-6

_TARGETED_ALWAYS_PRESERVED_FIELDS = (
    "narrative_role",
    "target_emotion",
    "target_emotional_intensity",
    "target_kinetic_energy",
    "required_visible_subjects",
)
_TARGETED_EVENT_FIELDS = (
    "content_description",
    "continuity_from_previous",
)


@dataclass(frozen=True, slots=True)
class RepairDomainAssessment:
    """Deterministic result of deciding which Slots may join a local repair.

    ``unrepairable`` is backend control state.  It is intentionally absent from
    the Arrangement Architect response contract: an impossible local domain is
    rejected before another model request is made.
    """

    original_slot_ids: frozenset[str]
    repair_slot_ids: frozenset[str]
    blocker_slot_ids: frozenset[str]
    unrepairable_slot_ids: frozenset[str]
    reason: str | None = None

    @property
    def unrepairable(self) -> bool:
        return bool(self.unrepairable_slot_ids)


def assess_repair_domain(
    slots: list[dict[str, Any]],
    target_slot_ids: set[str],
    *,
    blocker_slot_ids_by_slot: Mapping[str, Iterable[str]] | None = None,
    intrinsically_unrepairable_slot_ids: set[str] | None = None,
    video_description: dict[str, Any] | None = None,
    unavailable_source_segment_ids: set[str] | None = None,
    failed_source_segment_ids_by_slot: Mapping[str, Iterable[str]] | None = None,
) -> RepairDomainAssessment:
    """Expand a local repair through an explicit chain of adjacent blockers.

    A caller that proves a Slot's current chronological interval has no
    semantically usable Segment can identify the immediate neighbouring Slot
    that blocks access to a wider interval.  Every blocker edge must join two
    immediately adjacent Slots.  The closure therefore grows only through a
    continuous chain and never silently absorbs the Slots between a target and
    a distant blocker.

    A non-dialogue ``fixed_candidate`` is a hard boundary.  A dialogue anchor
    remains eligible because the existing Story refresh invalidates and
    reselects anchors after the repaired source assignment changes.
    """

    ordered_slot_ids = [str(slot.get("slot_id") or "") for slot in slots]
    if any(not slot_id for slot_id in ordered_slot_ids):
        raise ValueError("Every Slot must have a non-empty slot_id")
    if len(set(ordered_slot_ids)) != len(ordered_slot_ids):
        raise ValueError("Slot IDs must be unique when assessing a repair domain")

    slot_index = {
        slot_id: index for index, slot_id in enumerate(ordered_slot_ids)
    }
    unknown_targets = set(target_slot_ids) - set(slot_index)
    if unknown_targets:
        raise ValueError(
            "Repair domain contains unknown target Slots: "
            f"{sorted(unknown_targets)}"
        )

    original_slot_ids = frozenset(str(slot_id) for slot_id in target_slot_ids)
    repair_slot_ids = set(original_slot_ids)
    blocker_slot_ids: set[str] = set()
    unrepairable_slot_ids = {
        str(slot_id)
        for slot_id in intrinsically_unrepairable_slot_ids or set()
    }
    reasons: list[str] = []
    if unrepairable_slot_ids:
        reasons.append(
            "intrinsically unrepairable Slots: "
            f"{sorted(unrepairable_slot_ids)}"
        )
    unknown_intrinsic = unrepairable_slot_ids - set(slot_index)
    if unknown_intrinsic:
        reasons.append(
            "intrinsically unrepairable Slots are unknown: "
            f"{sorted(unknown_intrinsic)}"
        )
    non_target_intrinsic = unrepairable_slot_ids - set(original_slot_ids)
    if non_target_intrinsic:
        reasons.append(
            "intrinsically unrepairable Slots are not repair targets: "
            f"{sorted(non_target_intrinsic)}"
        )

    blockers_by_slot = {
        str(slot_id): {
            str(blocker_id)
            for blocker_id in blocker_ids
        }
        for slot_id, blocker_ids in (blocker_slot_ids_by_slot or {}).items()
    }
    slot_by_id = {
        str(slot["slot_id"]): slot
        for slot in slots
    }
    pending = list(sorted(original_slot_ids, key=slot_index.__getitem__))
    visited: set[str] = set()
    while pending:
        slot_id = pending.pop(0)
        if slot_id in visited:
            continue
        visited.add(slot_id)
        for blocker_id in sorted(
            blockers_by_slot.get(slot_id, set()),
            key=lambda value: slot_index.get(value, len(slot_index)),
        ):
            blocker_slot_ids.add(blocker_id)
            if blocker_id not in slot_index:
                unrepairable_slot_ids.add(slot_id)
                reasons.append(
                    f"{slot_id} names unknown blocker {blocker_id}"
                )
                continue
            if abs(slot_index[blocker_id] - slot_index[slot_id]) != 1:
                unrepairable_slot_ids.add(slot_id)
                reasons.append(
                    f"blocker {blocker_id} is not adjacent to {slot_id}"
                )
                continue
            blocker = slot_by_id[blocker_id]
            if (
                blocker.get("fixed_candidate") is not None
                and blocker.get("dialogue_anchor") is None
            ):
                unrepairable_slot_ids.add(slot_id)
                reasons.append(
                    f"{blocker_id} has a non-dialogue fixed candidate and blocks "
                    f"repair of {slot_id}"
                )
                continue
            if blocker_id in repair_slot_ids:
                continue
            repair_slot_ids.add(blocker_id)
            pending.append(blocker_id)

    if video_description is not None and not unrepairable_slot_ids:
        segment_index = {
            str(segment["segment_id"]): index
            for index, segment in enumerate(video_description["segments"])
        }
        segment_duration = {
            str(segment["segment_id"]): (
                float(segment["time_range"]["end_sec"])
                - float(segment["time_range"]["start_sec"])
            )
            for segment in video_description["segments"]
        }
        attempted_issue_reasons: list[str] = []

        while True:
            try:
                constraints = _targeted_slot_constraints(
                    slots,
                    repair_slot_ids,
                    video_description,
                    failed_slot_ids=set(original_slot_ids),
                    failed_source_segment_ids_by_slot=(
                        failed_source_segment_ids_by_slot
                    ),
                    unavailable_source_segment_ids=(
                        unavailable_source_segment_ids or set()
                    ),
                )
            except ValueError as exc:
                issue_slot_ids = set(original_slot_ids)
                issue_reasons = [str(exc)]
            else:
                issue_slot_ids: set[str] = set()
                issue_reasons: list[str] = []
                previous_index = -1
                for slot_id in ordered_slot_ids:
                    if slot_id not in repair_slot_ids:
                        continue
                    constraint = constraints[slot_id]
                    planned_duration = float(constraint["planned_duration_sec"])
                    eligible_positions = sorted(
                        segment_index[segment_id]
                        for segment_id in constraint["allowed_segment_ids"]
                        if segment_duration[segment_id]
                        > planned_duration + DURATION_TOLERANCE_SEC
                    )
                    next_position = next(
                        (
                            position
                            for position in eligible_positions
                            if position > previous_index
                        ),
                        None,
                    )
                    if next_position is None:
                        issue_slot_ids.add(slot_id)
                        if eligible_positions:
                            issue_reasons.append(
                                "no strictly increasing source-Segment assignment "
                                f"remains for {slot_id} after earlier repair Slots"
                            )
                        else:
                            issue_reasons.append(
                                f"no available source Segment for {slot_id} is longer "
                                f"than planned_duration_sec={planned_duration:.6f}"
                            )
                        continue
                    previous_index = next_position

            if not issue_slot_ids:
                break
            attempted_issue_reasons.extend(issue_reasons)

            issue_positions = sorted(slot_index[slot_id] for slot_id in issue_slot_ids)
            repair_positions = {
                slot_index[slot_id] for slot_id in repair_slot_ids
            }
            boundary_slot_ids: set[str] = set()
            for issue_position in issue_positions:
                component = {issue_position}
                while min(component) - 1 in repair_positions:
                    component.add(min(component) - 1)
                while max(component) + 1 in repair_positions:
                    component.add(max(component) + 1)
                for candidate_position in (min(component) - 1, max(component) + 1):
                    if candidate_position < 0 or candidate_position >= len(slots):
                        continue
                    candidate_slot_id = ordered_slot_ids[candidate_position]
                    if candidate_slot_id in repair_slot_ids:
                        continue
                    candidate_slot = slot_by_id[candidate_slot_id]
                    if (
                        candidate_slot.get("fixed_candidate") is not None
                        and candidate_slot.get("dialogue_anchor") is None
                    ):
                        continue
                    boundary_slot_ids.add(candidate_slot_id)

            if not boundary_slot_ids:
                unrepairable_slot_ids.update(original_slot_ids)
                reasons.extend(attempted_issue_reasons)
                break
            repair_slot_ids.update(boundary_slot_ids)
            blocker_slot_ids.update(boundary_slot_ids)

    reason = "; ".join(dict.fromkeys(reasons)) or None
    return RepairDomainAssessment(
        original_slot_ids=original_slot_ids,
        repair_slot_ids=frozenset(repair_slot_ids),
        blocker_slot_ids=frozenset(blocker_slot_ids),
        unrepairable_slot_ids=frozenset(unrepairable_slot_ids),
        reason=reason,
    )


def _targeted_failure_reasons_by_slot(
    failures: list[dict[str, Any]] | None,
) -> dict[str, set[str]]:
    reasons_by_slot: dict[str, set[str]] = {}
    for failure in failures or []:
        slot_id = str(failure.get("slot_id") or "")
        if not slot_id:
            continue
        reasons = reasons_by_slot.setdefault(slot_id, set())
        reason_code = str(failure.get("reason_code") or "")
        if reason_code:
            reasons.add(reason_code)
        candidate_rejections = failure.get("candidate_rejections") or []
        if not isinstance(candidate_rejections, list):
            continue
        for rejection in candidate_rejections:
            if not isinstance(rejection, dict):
                continue
            rejection_code = str(rejection.get("reason_code") or "")
            if rejection_code:
                reasons.add(rejection_code)
    return reasons_by_slot


def _repair_blocker_slot_ids_by_slot(
    failures: list[dict[str, Any]],
) -> dict[str, set[str]]:
    """Merge direct and batched blocker evidence emitted by Timeline Scout."""

    blockers_by_slot: dict[str, set[str]] = {}
    for failure in failures:
        nested = failure.get("blocker_slot_ids_by_slot")
        if isinstance(nested, Mapping):
            for raw_slot_id, raw_blocker_ids in nested.items():
                slot_id = str(raw_slot_id)
                if isinstance(raw_blocker_ids, str):
                    blocker_ids = [raw_blocker_ids]
                elif isinstance(raw_blocker_ids, Iterable):
                    blocker_ids = raw_blocker_ids
                else:
                    continue
                blockers_by_slot.setdefault(slot_id, set()).update(
                    str(blocker_id)
                    for blocker_id in blocker_ids
                    if str(blocker_id)
                )

        slot_id = str(failure.get("slot_id") or "")
        direct = failure.get("repair_blocker_slot_ids")
        if not slot_id or direct is None:
            continue
        if isinstance(direct, str):
            direct_ids = [direct]
        elif isinstance(direct, Iterable):
            direct_ids = direct
        else:
            continue
        blockers_by_slot.setdefault(slot_id, set()).update(
            str(blocker_id)
            for blocker_id in direct_ids
            if str(blocker_id)
        )
    return blockers_by_slot


def _failed_source_segment_ids_by_slot(
    failures: list[dict[str, Any]],
) -> dict[str, set[str]]:
    """Collect Slot-scoped failed Segment history from direct and batched fields."""

    failed_by_slot: dict[str, set[str]] = {}
    for failure in failures:
        nested = failure.get("failed_source_segment_ids_by_slot")
        if isinstance(nested, Mapping):
            for raw_slot_id, raw_segment_ids in nested.items():
                slot_id = str(raw_slot_id)
                if not slot_id:
                    continue
                if isinstance(raw_segment_ids, str):
                    segment_ids = [raw_segment_ids]
                elif isinstance(raw_segment_ids, Iterable):
                    segment_ids = raw_segment_ids
                else:
                    continue
                failed_by_slot.setdefault(slot_id, set()).update(
                    str(segment_id)
                    for segment_id in segment_ids
                    if str(segment_id)
                )

        slot_id = str(failure.get("slot_id") or "")
        direct = failure.get("failed_source_segment_ids")
        if not slot_id or direct is None:
            continue
        if isinstance(direct, str):
            direct_ids = [direct]
        elif isinstance(direct, Iterable):
            direct_ids = direct
        else:
            continue
        failed_by_slot.setdefault(slot_id, set()).update(
            str(segment_id)
            for segment_id in direct_ids
            if str(segment_id)
        )
    return failed_by_slot


def _targeted_retry_note(
    slot_ids: set[str],
    failures: list[dict[str, Any]],
) -> str:
    reasons_by_slot = _targeted_failure_reasons_by_slot(failures)
    policies: list[str] = []
    relevance_code = PromptFailureCode.VISUAL_SLOT_NOT_RELEVANT.value
    for slot_id in sorted(slot_ids):
        reasons = reasons_by_slot.get(slot_id, set())
        mutable = ["source_segment_ids"]
        if relevance_code in reasons:
            mutable.extend(_TARGETED_EVENT_FIELDS)
        reason_text = ", ".join(sorted(reasons)) or "collateral chronology repair"
        policies.append(
            f"{slot_id}: reasons=[{reason_text}]; mutable_fields={mutable}"
        )
    return (
        "Executable targeted-repair policy (this overrides broader redesign advice): "
        + "; ".join(policies)
        + ". Preserve every other field exactly. In particular, never delete, rename, "
        "replace, or weaken required_visible_subjects. Failed Slots must move off their "
        "previous source Segment assignment; changing prose is not a repair for missing "
        "subjects, static footage, or insufficient duration."
    )


def _request_metadata(request: PlannersRequest) -> dict[str, Any]:
    return {
        "instruction": request.prompt,
        "prompt_type": request.prompt_type,
        "video_title": request.video_title or request.video_path.stem,
        "target_duration_sec": request.target_output_length_sec,
    }


def _source_story_context(
    video_description: dict[str, Any],
    video_summary: dict[str, Any],
) -> dict[str, Any]:
    return {
        "source": video_description["source"],
        "story_summary": video_summary,
        "segments": [
            {
                "segment_id": segment["segment_id"],
                "time_range": segment["time_range"],
                "has_dialogue": segment["has_dialogue"],
                "speech_mode": segment["speech_mode"],
                "content_type": segment["content_type"],
                "timeline_role": segment["timeline_role"],
                "segment_summary": segment["segment_summary"],
                "narrative_function": segment["narrative_function"],
                "emotional_tone": segment["emotional_tone"],
                "emotional_intensity": segment["emotional_intensity"],
                "appearing_characters": segment["appearing_characters"],
            }
            for segment in video_description["segments"]
        ],
    }


def _average_target_tolerance(
    target_duration_sec: float,
    target_clip_duration_sec: float,
) -> float:
    maximum_slot_count = max(
        1,
        int(target_duration_sec // MIN_SLOT_DURATION_SEC),
    )
    nearest_integer_count_error = min(
        abs(
            target_duration_sec / slot_count
            - target_clip_duration_sec
        )
        for slot_count in range(1, maximum_slot_count + 1)
    )
    return max(
        target_clip_duration_sec * MAX_AVERAGE_TARGET_ERROR_RATIO,
        nearest_integer_count_error + DURATION_TOLERANCE_SEC,
    )


def _validate_slots(
    parsed: dict[str, Any],
    target_duration_sec: float,
    target_clip_duration_sec: float,
    video_description: dict[str, Any],
    unavailable_source_segment_ids: set[str],
    hard_forbidden_assignments: Mapping[
        str,
        Iterable[Iterable[str]],
    ],
) -> list[dict[str, Any]]:
    raw = parsed.get("slots")
    if not isinstance(raw, list) or not raw:
        raise ValueError("Expected at least one edit slot")
    slots: list[dict[str, Any]] = []
    source_segments = video_description["segments"]
    segment_order = {
        str(segment["segment_id"]): index
        for index, segment in enumerate(source_segments)
    }
    segment_duration_by_id = {
        str(segment["segment_id"]): (
            float(segment["time_range"]["end_sec"])
            - float(segment["time_range"]["start_sec"])
        )
        for segment in source_segments
    }
    previous_segment_end_index = -1
    for index, item in enumerate(raw, 1):
        slot_id = f"slot_{index:02d}"
        if not isinstance(item, dict):
            raise ValueError(f"Slot {index} must be an object")
        description = str(item.get("content_description") or "").strip()
        if not description:
            raise ValueError(f"Slot {index} has no content description")
        duration = float(item.get("desired_duration_sec") or 0)
        if duration < MIN_SLOT_DURATION_SEC:
            raise ValueError(
                f"Slot {index} desired duration must be at least "
                f"{MIN_SLOT_DURATION_SEC:.1f} seconds"
            )
        maximum_duration = (
            target_clip_duration_sec * MAX_TARGET_DURATION_RATIO
        )
        if duration > maximum_duration:
            raise ValueError(
                f"Slot {index} desired duration must not exceed "
                f"{maximum_duration:.3f} seconds for "
                f"target_clip_duration_sec={target_clip_duration_sec:.3f}; "
                "long dialogue must span multiple visual Slots as a start-aligned L-cut"
            )
        segment_ids = [
            str(value).strip()
            for value in item.get("source_segment_ids") or []
            if str(value).strip()
        ]
        if not segment_ids or any(
            segment_id not in segment_order for segment_id in segment_ids
        ):
            raise ValueError(f"Slot {index} must reference valid source Segment IDs")
        if any(
            segment_id in unavailable_source_segment_ids
            for segment_id in segment_ids
        ):
            raise ValueError(f"Slot {index} reuses a visually disproven source Segment")
        segment_positions = [segment_order[segment_id] for segment_id in segment_ids]
        if len(set(segment_positions)) != len(segment_positions):
            raise ValueError(f"Slot {index} repeats a source Segment ID")
        if segment_positions != sorted(segment_positions):
            raise ValueError(
                f"Slot {index} source_segment_ids must follow source order"
            )
        forbidden_for_slot = {
            tuple(str(segment_id) for segment_id in assignment)
            for assignment in hard_forbidden_assignments.get(slot_id, ())
        }
        if tuple(segment_ids) in forbidden_for_slot:
            forbidden_display = [
                list(assignment)
                for assignment in sorted(forbidden_for_slot)
            ]
            raise ValueError(
                f"Slot {index} ({slot_id}) attempted assignment={segment_ids!r}; "
                f"forbidden assignments={forbidden_display!r}"
            )
        if not any(
            segment_duration_by_id[segment_id] > duration + DURATION_TOLERANCE_SEC
            for segment_id in segment_ids
        ):
            raise ValueError(
                f"Slot {index} must assign at least one source Segment longer than "
                f"desired_duration_sec={duration:.6f}"
            )
        segment_start_index = min(
            segment_order[segment_id] for segment_id in segment_ids
        )
        segment_end_index = max(
            segment_order[segment_id] for segment_id in segment_ids
        )
        if segment_start_index <= previous_segment_end_index:
            raise ValueError(
                "Slot source Segment ranges must be in strictly increasing source order"
            )
        previous_segment_end_index = segment_end_index
        raw_required_subjects = item.get("required_visible_subjects")
        if not isinstance(raw_required_subjects, list):
            raise ValueError(f"Slot {index} must list required_visible_subjects")
        required_subjects = [
            str(value).strip()
            for value in raw_required_subjects
            if str(value).strip()
        ]
        slots.append(
            {
                "slot_id": slot_id,
                "narrative_role": str(item["narrative_role"]),
                "content_description": description,
                "target_emotion": str(item["target_emotion"]),
                "target_emotional_intensity": max(
                    0.0, min(1.0, float(item["target_emotional_intensity"]))
                ),
                "target_kinetic_energy": max(
                    0.0, min(1.0, float(item["target_kinetic_energy"]))
                ),
                "desired_duration_sec": duration,
                "continuity_from_previous": str(item["continuity_from_previous"]),
                "source_segment_ids": segment_ids,
                "required_visible_subjects": required_subjects,
            }
        )
    duration_total = sum(float(slot["desired_duration_sec"]) for slot in slots)
    if abs(duration_total - target_duration_sec) > MAX_DURATION_TOTAL_ERROR_SEC:
        raise ValueError(
            "Slot desired durations must total the requested output duration within "
            f"{MAX_DURATION_TOTAL_ERROR_SEC:.1f} seconds; got {duration_total:.3f} "
            f"for target {target_duration_sec:.3f}"
        )
    average_duration = duration_total / len(slots)
    maximum_average_error = _average_target_tolerance(
        target_duration_sec,
        target_clip_duration_sec,
    )
    if abs(average_duration - target_clip_duration_sec) > maximum_average_error:
        raise ValueError(
            "Average Slot duration must stay close to "
            f"target_clip_duration_sec={target_clip_duration_sec:.3f}; "
            f"got {average_duration:.3f} seconds across {len(slots)} Slots"
        )
    return slots


def plan_edit_slots(
    request: PlannersRequest,
    music_profile: dict[str, Any],
    config: LLMConfig,
    context: WorkflowContext,
    *,
    target_clip_duration_sec: float,
    candidates_per_slot: int = 3,
) -> list[dict[str, Any]]:
    prime_arrangement_context(request, music_profile, context)
    return _plan_edit_slots_from_context(
        request,
        music_profile,
        config,
        context,
        target_clip_duration_sec=target_clip_duration_sec,
        candidates_per_slot=candidates_per_slot,
    )


def prime_arrangement_context(
    request: PlannersRequest,
    music_profile: dict[str, Any],
    context: WorkflowContext,
) -> None:
    """Rebuild request-derived in-memory context without a model call."""

    context.set_artifact("request", _request_metadata(request))
    context.set_artifact(
        "music_profile",
        compact_music_profile(music_profile),
    )
    video_description = context.get_artifact("video_description")
    if video_description is None:
        raise RuntimeError("Video description must be available before Slot arrangement")
    video_summary = context.get_artifact("video_summary")
    if video_summary is None:
        raise RuntimeError("Video summary must be available before Slot arrangement")
    context.set_artifact(
        "source_story_context",
        _source_story_context(video_description, video_summary),
    )
    return None


def _plan_edit_slots_from_context(
    request: PlannersRequest,
    music_profile: dict[str, Any],
    config: LLMConfig,
    context: WorkflowContext,
    *,
    target_clip_duration_sec: float,
    candidates_per_slot: int = 3,
) -> list[dict[str, Any]]:
    video_description = context.get_artifact("video_description")
    if video_description is None:
        raise RuntimeError("Video description must be available before Slot arrangement")
    video_summary = context.get_artifact("video_summary")
    if video_summary is None:
        raise RuntimeError("Video summary must be available before Slot arrangement")
    planners_feedback = context.get_artifact("planners_feedback")
    unavailable_source_segment_ids = {
        str(value)
        for value in (
            (planners_feedback or {}).get("unavailable_source_segment_ids") or []
        )
    }
    unavailable_source_segment_ids.update(
        str(value)
        for value in (
            context.get_artifact("unavailable_source_segment_ids") or []
        )
    )
    excluded_segment_ids = unavailable_source_segment_ids
    segment_order = {
        str(segment["segment_id"]): index
        for index, segment in enumerate(video_description["segments"])
    }
    hard_forbidden_assignments: dict[str, set[tuple[str, ...]]] = {}
    for failed in (planners_feedback or {}).get("failed_slots") or []:
        if not is_hard_failed_slot_record(
            failed,
            candidates_per_slot=candidates_per_slot,
        ):
            continue
        slot_id = str(failed.get("slot_id") or "")
        assignment = tuple(
            sorted(
                (
                    str(value)
                    for value in failed.get("source_segment_ids") or []
                    if str(value)
                ),
                key=lambda segment_id: segment_order.get(
                    segment_id,
                    len(segment_order),
                ),
            )
        )
        if not slot_id or not assignment:
            continue
        hard_forbidden_assignments.setdefault(slot_id, set()).add(assignment)
    prompt_forbidden_assignments = {
        slot_id: [
            list(assignment)
            for assignment in sorted(assignments)
        ]
        for slot_id, assignments in sorted(hard_forbidden_assignments.items())
    }
    retry_note = ""
    if planners_feedback:
        retry_note = (
            "\nThis is a redesign after an infeasible candidate path. Correct the failure using "
            "the compact hard_forbidden_assignments table. Never use a Segment omitted "
            "from the allowed domain. A partial candidate shortage is diagnostic only and "
            "does not forbid its assignment. Preserve chronology.\n"
        )
    package = prompt_registry.build(
        PromptStage.PLANNERS,
        PromptTask.SLOT_ARRANGEMENT,
        SlotArrangementDetails(
            target_duration_sec=request.target_output_length_sec,
            target_clip_duration_sec=target_clip_duration_sec,
            allowed_segment_ids=[
                str(segment["segment_id"])
                for segment in video_description["segments"]
                if str(segment["segment_id"]) not in excluded_segment_ids
            ],
            retry_note=retry_note,
            mode="full",
            existing_slots=[],
            target_slot_constraints={},
            rejection_feedback=[],
            hard_forbidden_assignments=prompt_forbidden_assignments,
        ),
    )
    return context.call_prompt(
        package=package,
        config=config,
        validate_business=lambda parsed: _validate_slots(
            parsed,
            request.target_output_length_sec,
            target_clip_duration_sec,
            video_description,
            excluded_segment_ids,
            hard_forbidden_assignments,
        ),
    )


def _targeted_slot_constraints(
    slots: list[dict[str, Any]],
    target_slot_ids: set[str],
    video_description: dict[str, Any],
    *,
    failed_slot_ids: set[str] | None = None,
    unavailable_source_segment_ids: set[str] | None = None,
    failed_source_segment_ids_by_slot: Mapping[str, Iterable[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    source_segments = video_description["segments"]
    segment_ids = [str(segment["segment_id"]) for segment in source_segments]
    segment_order = {
        segment_id: index for index, segment_id in enumerate(segment_ids)
    }

    def start_index(slot: dict[str, Any]) -> int:
        return min(
            segment_order[str(segment_id)]
            for segment_id in slot["source_segment_ids"]
        )

    def end_index(slot: dict[str, Any]) -> int:
        return max(
            segment_order[str(segment_id)]
            for segment_id in slot["source_segment_ids"]
        )

    constraints: dict[str, dict[str, Any]] = {}
    failed_slot_ids = failed_slot_ids or set()
    unavailable_source_segment_ids = unavailable_source_segment_ids or set()
    failed_source_segments = {
        str(slot_id): {
            str(segment_id)
            for segment_id in segment_ids
            if str(segment_id)
        }
        for slot_id, segment_ids in (
            failed_source_segment_ids_by_slot or {}
        ).items()
    }
    for index, slot in enumerate(slots):
        slot_id = str(slot["slot_id"])
        if slot_id not in target_slot_ids:
            continue
        previous_slot = next(
            (
                slots[position]
                for position in range(index - 1, -1, -1)
                if str(slots[position]["slot_id"]) not in target_slot_ids
            ),
            None,
        )
        next_slot = next(
            (
                slots[position]
                for position in range(index + 1, len(slots))
                if str(slots[position]["slot_id"]) not in target_slot_ids
            ),
            None,
        )
        lower = end_index(previous_slot) + 1 if previous_slot is not None else 0
        upper = (
            start_index(next_slot) - 1
            if next_slot is not None
            else len(segment_ids) - 1
        )
        if lower > upper:
            raise ValueError(
                f"No chronological Segment interval remains for {slot_id}"
            )
        forbidden_for_slot = set(failed_source_segments.get(slot_id, set()))
        if slot_id in failed_slot_ids:
            forbidden_for_slot.update(
                str(value)
                for value in slot.get("source_segment_ids") or []
                if str(value)
            )
        constraints[slot_id] = {
            "desired_duration_sec": float(slot["desired_duration_sec"]),
            "planned_duration_sec": float(slot["planned_duration_sec"]),
            "allowed_segment_ids": [
                segment_id
                for segment_id in segment_ids[lower : upper + 1]
                if segment_id not in unavailable_source_segment_ids
                if segment_id not in forbidden_for_slot
            ],
            "previous_fixed_slot": (
                {
                    "slot_id": previous_slot["slot_id"],
                    "source_segment_ids": previous_slot["source_segment_ids"],
                    "content_description": previous_slot["content_description"],
                }
                if previous_slot is not None
                else None
            ),
            "next_fixed_slot": (
                {
                    "slot_id": next_slot["slot_id"],
                    "source_segment_ids": next_slot["source_segment_ids"],
                    "content_description": next_slot["content_description"],
                }
                if next_slot is not None
                else None
            ),
            "previous_source_segment_ids": list(slot["source_segment_ids"]),
        }
    return constraints


def _validate_targeted_slots(
    parsed: dict[str, Any],
    slots: list[dict[str, Any]],
    constraints: dict[str, dict[str, Any]],
    video_description: dict[str, Any],
    *,
    failures: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    raw_slots = parsed.get("slots")
    if not isinstance(raw_slots, list):
        raise ValueError("Targeted Slot redesign must contain a slots array")
    expected_ids = set(constraints)
    original_by_id = {str(slot["slot_id"]): slot for slot in slots}
    reasons_by_slot = _targeted_failure_reasons_by_slot(failures)
    relevance_code = PromptFailureCode.VISUAL_SLOT_NOT_RELEVANT.value
    replacements: dict[str, dict[str, Any]] = {}
    for item in raw_slots:
        if not isinstance(item, dict):
            raise ValueError("Each targeted Slot replacement must be an object")
        slot_id = str(item.get("slot_id") or "")
        if slot_id not in expected_ids or slot_id in replacements:
            raise ValueError(f"Unexpected targeted Slot ID: {slot_id}")
        constraint = constraints[slot_id]
        duration = float(item["desired_duration_sec"])
        if abs(duration - float(constraint["desired_duration_sec"])) > DURATION_TOLERANCE_SEC:
            raise ValueError(f"Targeted redesign changed desired duration for {slot_id}")
        planned_duration = float(item["planned_duration_sec"])
        if (
            abs(
                planned_duration
                - float(constraint["planned_duration_sec"])
            )
            > DURATION_TOLERANCE_SEC
        ):
            raise ValueError(f"Targeted redesign changed planned duration for {slot_id}")
        segment_ids = [
            str(value).strip()
            for value in item.get("source_segment_ids") or []
            if str(value).strip()
        ]
        allowed = set(constraint["allowed_segment_ids"])
        if not segment_ids or any(segment_id not in allowed for segment_id in segment_ids):
            raise ValueError(
                f"Targeted redesign placed {slot_id} outside its chronological interval"
            )
        required_subjects = [
            str(value).strip()
            for value in item.get("required_visible_subjects") or []
            if str(value).strip()
        ]
        replacement = {
            "slot_id": slot_id,
            "narrative_role": str(item["narrative_role"]),
            "content_description": str(item["content_description"]).strip(),
            "target_emotion": str(item["target_emotion"]),
            "target_emotional_intensity": max(
                0.0, min(1.0, float(item["target_emotional_intensity"]))
            ),
            "target_kinetic_energy": max(
                0.0, min(1.0, float(item["target_kinetic_energy"]))
            ),
            "desired_duration_sec": duration,
            "planned_duration_sec": planned_duration,
            "continuity_from_previous": str(item["continuity_from_previous"]),
            "source_segment_ids": segment_ids,
            "required_visible_subjects": required_subjects,
        }
        if not replacement["content_description"]:
            raise ValueError(f"Targeted redesign left {slot_id} without visible content")
        if failures is not None:
            original = original_by_id[slot_id]
            preserved_fields = list(_TARGETED_ALWAYS_PRESERVED_FIELDS)
            if relevance_code not in reasons_by_slot.get(slot_id, set()):
                preserved_fields.extend(_TARGETED_EVENT_FIELDS)
            for field_name in preserved_fields:
                replacement_value: Any = replacement[field_name]
                original_value: Any = original[field_name]
                if field_name == "required_visible_subjects":
                    original_value = [
                        str(value).strip()
                        for value in original_value or []
                        if str(value).strip()
                    ]
                if field_name in {
                    "target_emotional_intensity",
                    "target_kinetic_energy",
                }:
                    unchanged = (
                        abs(float(replacement_value) - float(original_value))
                        <= DURATION_TOLERANCE_SEC
                    )
                else:
                    unchanged = replacement_value == original_value
                if not unchanged:
                    raise ValueError(
                        f"Targeted redesign changed protected {field_name} for "
                        f"{slot_id}; repair the reported source evidence instead"
                    )
            if reasons_by_slot.get(slot_id):
                previous_segment_ids = {
                    str(value)
                    for value in constraint["previous_source_segment_ids"]
                }
                if previous_segment_ids.intersection(segment_ids):
                    raise ValueError(
                        f"Targeted redesign must move {slot_id} to a different source "
                        "Segment after its previous evidence failed"
                    )
        replacements[slot_id] = replacement
    if set(replacements) != expected_ids:
        raise ValueError(
            f"Targeted redesign omitted Slots: {sorted(expected_ids - set(replacements))}"
        )

    merged: list[dict[str, Any]] = []
    for slot in slots:
        slot_id = str(slot["slot_id"])
        if slot_id not in replacements:
            merged.append(slot)
            continue
        updated = dict(slot)
        updated.update(replacements[slot_id])
        merged.append(updated)

    segment_order = {
        str(segment["segment_id"]): index
        for index, segment in enumerate(video_description["segments"])
    }
    segment_duration_by_id = {
        str(segment["segment_id"]): (
            float(segment["time_range"]["end_sec"])
            - float(segment["time_range"]["start_sec"])
        )
        for segment in video_description["segments"]
    }
    previous_end_index = -1
    for slot in merged:
        if not any(
            segment_duration_by_id[str(segment_id)]
            > float(slot["planned_duration_sec"]) + DURATION_TOLERANCE_SEC
            for segment_id in slot["source_segment_ids"]
        ):
            raise ValueError(
                f"Targeted Slot {slot['slot_id']} must assign at least one source "
                "Segment longer than planned_duration_sec"
            )
        current_start_index = min(
            segment_order[str(segment_id)]
            for segment_id in slot["source_segment_ids"]
        )
        current_end_index = max(
            segment_order[str(segment_id)]
            for segment_id in slot["source_segment_ids"]
        )
        if current_start_index <= previous_end_index:
            raise ValueError(
                "Targeted Slot replacements violate strictly increasing source order"
            )
        previous_end_index = current_end_index
    return merged


def redesign_edit_slots(
    slots: list[dict[str, Any]],
    failures: list[dict[str, Any]],
    config: LLMConfig,
    context: WorkflowContext,
) -> tuple[list[dict[str, Any]], set[str]]:
    target_slot_ids = {
        str(failure["slot_id"]) for failure in failures
    }
    if not target_slot_ids:
        return slots, set()
    video_description = context.get_artifact("video_description")
    if video_description is None:
        raise RuntimeError("Video description must be available for targeted Slot redesign")
    planners_feedback = context.get_artifact("planners_feedback")
    unavailable_source_segment_ids = {
        str(value)
        for value in (
            (planners_feedback or {}).get("unavailable_source_segment_ids") or []
        )
    }
    unavailable_source_segment_ids.update(
        str(value)
        for value in (
            context.get_artifact("unavailable_source_segment_ids") or []
        )
    )
    failed_source_segment_ids_by_slot: dict[str, set[str]] = {
        str(slot["slot_id"]): {
            str(segment_id)
            for segment_id in slot.get("source_segment_ids") or []
        }
        for slot in slots
        if str(slot["slot_id"]) in target_slot_ids
    }
    for slot_id, segment_ids in _failed_source_segment_ids_by_slot(
        failures
    ).items():
        failed_source_segment_ids_by_slot.setdefault(slot_id, set()).update(
            segment_ids
        )
    repair_domain = assess_repair_domain(
        slots,
        target_slot_ids,
        blocker_slot_ids_by_slot=_repair_blocker_slot_ids_by_slot(failures),
        video_description=video_description,
        unavailable_source_segment_ids=unavailable_source_segment_ids,
        failed_source_segment_ids_by_slot=failed_source_segment_ids_by_slot,
    )
    if repair_domain.unrepairable:
        raise TargetedRepairUnrepairableError(
            {
                "reason": repair_domain.reason,
                "failed_slot_ids": sorted(repair_domain.original_slot_ids),
                "repair_slot_ids": sorted(repair_domain.repair_slot_ids),
                "blocker_slot_ids": sorted(repair_domain.blocker_slot_ids),
                "unrepairable_slot_ids": sorted(
                    repair_domain.unrepairable_slot_ids
                ),
                "unavailable_source_segment_ids": sorted(
                    unavailable_source_segment_ids
                ),
            }
        )
    expanded_slot_ids = set(repair_domain.repair_slot_ids)
    constraints = _targeted_slot_constraints(
        slots,
        expanded_slot_ids,
        video_description,
        failed_slot_ids=target_slot_ids,
        unavailable_source_segment_ids=unavailable_source_segment_ids,
        failed_source_segment_ids_by_slot=failed_source_segment_ids_by_slot,
    )
    if expanded_slot_ids != target_slot_ids:
        log_event(
            "WARNING",
            "aster.arrangement",
            "fallback.apply",
            "Expanded a backend-validated repair domain to adjacent Slots",
            failed_slot_ids=sorted(target_slot_ids),
            replanned_slot_ids=sorted(expanded_slot_ids),
            allowed_segment_ids=sorted(
                {
                    segment_id
                    for constraint in constraints.values()
                    for segment_id in constraint["allowed_segment_ids"]
                }
            ),
        )
    package = prompt_registry.build(
        PromptStage.PLANNERS,
        PromptTask.SLOT_ARRANGEMENT,
        SlotArrangementDetails(
            target_duration_sec=sum(
                float(slot["planned_duration_sec"]) for slot in slots
            ),
            target_clip_duration_sec=(
                sum(float(slot["planned_duration_sec"]) for slot in slots)
                / len(slots)
            ),
            allowed_segment_ids=[],
            retry_note=_targeted_retry_note(expanded_slot_ids, failures),
            mode="targeted",
            existing_slots=slots,
            target_slot_constraints=constraints,
            rejection_feedback=failures,
            hard_forbidden_assignments={},
        ),
    )
    redesigned = context.call_prompt(
        package=package,
        config=config,
        validate_business=lambda parsed: _validate_targeted_slots(
            parsed,
            slots,
            constraints,
            video_description,
            failures=failures,
        ),
    )
    context.set_artifact("edit_plan", redesigned)
    return redesigned, expanded_slot_ids


def align_slots_to_music(
    slots: list[dict[str, Any]],
    music_profile: dict[str, Any],
    total_duration_sec: float,
    output_fps: int,
    target_clip_duration_sec: float,
) -> list[dict[str, Any]]:
    weights = [max(0.1, float(slot["desired_duration_sec"])) for slot in slots]
    scale = total_duration_sec / sum(weights)
    elapsed = 0.0
    ideal: list[float] = []
    for weight in weights[:-1]:
        elapsed += weight * scale
        ideal.append(elapsed)
    accents = music_profile["accents_sec"]
    maximum_clip_duration_sec = (
        target_clip_duration_sec * MAX_TARGET_DURATION_RATIO
    )
    try:
        boundaries = _globally_align_boundaries(
            ideal,
            accents,
            total_duration_sec,
            output_fps,
            max_boundary_shift_sec=MAX_MUSIC_BOUNDARY_SHIFT_SEC,
            max_clip_duration_sec=maximum_clip_duration_sec,
        )
    except ValueError:
        try:
            boundaries = _globally_align_boundaries(
                ideal,
                music_profile["beats_sec"],
                total_duration_sec,
                output_fps,
                max_boundary_shift_sec=MAX_MUSIC_BOUNDARY_SHIFT_SEC,
                max_clip_duration_sec=maximum_clip_duration_sec,
            )
        except ValueError:
            boundaries = [
                round(value * output_fps) / output_fps
                for value in ideal
            ]
    aligned_durations = [
        end - start
        for start, end in zip(
            [0.0, *boundaries],
            [*boundaries, total_duration_sec],
            strict=True,
        )
    ]
    if any(
        duration > maximum_clip_duration_sec + 1.0 / output_fps
        for duration in aligned_durations
    ):
        raise ValueError(
            "Music alignment produced a visual Slot longer than "
            f"{maximum_clip_duration_sec:.3f} seconds"
        )
    edges = [0.0, *boundaries, total_duration_sec]
    aligned: list[dict[str, Any]] = []
    for slot, start, end in zip(slots, edges[:-1], edges[1:], strict=True):
        item = dict(slot)
        item["output_start_sec"] = round(start, 6)
        item["output_end_sec"] = round(end, 6)
        item["planned_duration_sec"] = round(end - start, 6)
        aligned.append(item)
    return aligned


def _globally_align_boundaries(
    ideal: list[float],
    candidates: list[float],
    total_duration_sec: float,
    output_fps: int,
    min_clip_duration_sec: float = 1.5,
    max_boundary_shift_sec: float | None = None,
    max_clip_duration_sec: float | None = None,
) -> list[float]:
    if not ideal:
        return []
    values = sorted(
        {
            round(float(value) * output_fps) / output_fps
            for value in candidates
            if min_clip_duration_sec
            <= float(value)
            <= total_duration_sec - min_clip_duration_sec
        }
    )
    if len(values) < len(ideal):
        raise ValueError("Not enough musical accents to align all edit boundaries")
    states: dict[int, tuple[float, list[float]]] = {}
    for index, value in enumerate(values):
        if (
            value >= min_clip_duration_sec
            and (
                max_clip_duration_sec is None
                or value <= max_clip_duration_sec
            )
            and (
                max_boundary_shift_sec is None
                or abs(value - ideal[0]) <= max_boundary_shift_sec
            )
        ):
            states[index] = (abs(value - ideal[0]), [value])
    for boundary_index in range(1, len(ideal)):
        next_states: dict[int, tuple[float, list[float]]] = {}
        for index, value in enumerate(values):
            if (
                max_boundary_shift_sec is not None
                and abs(value - ideal[boundary_index]) > max_boundary_shift_sec
            ):
                continue
            best: tuple[float, list[float]] | None = None
            for previous_index, (cost, path) in states.items():
                clip_duration = value - path[-1]
                if (
                    previous_index >= index
                    or clip_duration < min_clip_duration_sec
                    or (
                        max_clip_duration_sec is not None
                        and clip_duration > max_clip_duration_sec
                    )
                ):
                    continue
                proposal = (cost + abs(value - ideal[boundary_index]), [*path, value])
                if best is None or proposal[0] < best[0]:
                    best = proposal
            if best is not None:
                next_states[index] = best
        states = next_states
        if not states:
            raise ValueError("No monotonic musical-boundary path satisfies minimum clip length")
    feasible = [
        state
        for state in states.values()
        if total_duration_sec - state[1][-1] >= min_clip_duration_sec
        and (
            max_clip_duration_sec is None
            or total_duration_sec - state[1][-1] <= max_clip_duration_sec
        )
    ]
    if not feasible:
        raise ValueError("No musical-boundary path leaves room for the final clip")
    return min(feasible, key=lambda state: state[0])[1]


class ArrangementArchitectAgent:
    """A agent: arrange Slots, pacing, emotion, and narrative structure."""

    def __init__(self, config: AppConfig, context: WorkflowContext) -> None:
        self.config = config
        self.context = context

    def profile_music(
        self,
        music_memory: dict[str, Any],
        target_duration_sec: float,
        output_path: Path,
    ) -> dict[str, Any]:
        profile = build_music_profile(music_memory, target_duration_sec)
        write_music_profile(output_path, profile)
        self.context.set_artifact(
            "music_profile",
            compact_music_profile(profile),
        )
        return profile

    def arrange(
        self,
        request: PlannersRequest,
        music_profile: dict[str, Any],
    ) -> list[dict[str, Any]]:
        slots = plan_edit_slots(
            request,
            music_profile,
            self.config.llm,
            self.context,
            target_clip_duration_sec=(
                self.config.planners.arrangement_architect.target_clip_duration_sec
            ),
            candidates_per_slot=(
                self.config.planners.candidate_retrieval.candidates_per_slot
            ),
        )
        return align_slots_to_music(
            slots,
            music_profile,
            request.target_output_length_sec,
            self.config.renderer.fps,
            self.config.planners.arrangement_architect.target_clip_duration_sec,
        )

    def repair(
        self,
        slots: list[dict[str, Any]],
        failures: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], set[str]]:
        return redesign_edit_slots(
            slots,
            failures,
            self.config.llm,
            self.context,
        )


__all__ = [
    "ArrangementArchitectAgent",
    "RepairDomainAssessment",
    "assess_repair_domain",
]
