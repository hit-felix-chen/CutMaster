from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class PromptFailureCode(StrEnum):
    EXTERNAL_PROCESS_FAILED = "external_process_failed"
    WORKFLOW_FAILED = "workflow_failed"
    MODEL_CALL_FAILED = "model_call_failed"
    MODEL_REQUEST_FAILED = "model_request_failed"
    MODEL_RETRY_EXHAUSTED = "model_retry_exhausted"
    MODEL_RETRY_SCHEDULED = "model_retry_scheduled"
    PLANNERS_STAGE_ATTEMPT_INFEASIBLE = "planners_stage_attempt_infeasible"
    RESPONSE_VALIDATION_FAILED = "response_validation_failed"
    CANDIDATE_RETRIEVAL_FAILED = "candidate_retrieval_failed"
    SOURCE_SEGMENTS_TOO_SHORT = "source_segments_too_short"
    VISUALLY_STATIC = "visually_static"
    REQUIRED_SUBJECT_NOT_VISUALLY_CONFIRMED = (
        "required_subject_not_visually_confirmed"
    )
    DUPLICATE_CANDIDATE_RANGE = "duplicate_candidate_range"
    NO_CANDIDATE_PASSED_VISUAL_DIAGNOSTICS = (
        "no_candidate_passed_visual_diagnostics"
    )
    INSUFFICIENT_VISUALLY_GROUNDED_CANDIDATES = (
        "insufficient_visually_grounded_candidates"
    )
    SOURCE_CHRONOLOGY_OR_OVERLAP = "source_chronology_or_overlap"
    PATCH_DEGRADES_OR_REQUIRES_UNSCORED_PATH = (
        "patch_degrades_or_requires_unscored_path"
    )
    PATCH_EXCLUDED_BY_MAXIMAL_FEASIBLE_SUBSET = (
        "patch_excluded_by_maximal_feasible_subset"
    )
    PROVIDER_IMAGE_INSPECTION_FAILED = "provider_image_inspection_failed"
    PROVIDER_DATA_INSPECTION_FAILED = "provider_data_inspection_failed"
    PROVIDER_REQUEST_LIMIT_EXCEEDED = "provider_request_limit_exceeded"


@dataclass(frozen=True)
class PromptFailureDefinition:
    diagnosis: str
    repair_requirement: str


PROMPT_FAILURE_CATALOG: dict[
    PromptFailureCode,
    PromptFailureDefinition,
] = {
    PromptFailureCode.EXTERNAL_PROCESS_FAILED: PromptFailureDefinition(
        diagnosis=(
            "External process {operation} failed with {error_type}: {error_message}"
        ),
        repair_requirement=(
            "Inspect the subprocess exit status and inputs, correct the underlying "
            "media or runtime failure, and rerun the failed operation."
        ),
    ),
    PromptFailureCode.WORKFLOW_FAILED: PromptFailureDefinition(
        diagnosis=(
            "The CutMaster workflow stopped with {error_type}: {error_message}"
        ),
        repair_requirement=(
            "Resolve the reported root cause, preserve valid cached stage outputs, and "
            "restart the workflow from the earliest incomplete stage."
        ),
    ),
    PromptFailureCode.MODEL_CALL_FAILED: PromptFailureDefinition(
        diagnosis=(
            "The {task} model call failed with {error_type}: {error_message}"
        ),
        repair_requirement=(
            "Retry the same task using the original response contract. If validation "
            "feedback is supplied, correct every reported violation before returning."
        ),
    ),
    PromptFailureCode.MODEL_REQUEST_FAILED: PromptFailureDefinition(
        diagnosis=(
            "Model request {operation} failed with {error_type}: {error_message}"
        ),
        repair_requirement=(
            "Retry with the same response contract after correcting the provider, "
            "transport, authentication, content-inspection, or input issue identified "
            "by the error."
        ),
    ),
    PromptFailureCode.MODEL_RETRY_EXHAUSTED: PromptFailureDefinition(
        diagnosis=(
            "Model transaction {operation} still failed after {max_attempts} attempts: "
            "{error_message}"
        ),
        repair_requirement=(
            "Stop automatic retries and surface the accumulated validation or provider "
            "diagnostics to the caller for ASTER redesign or configuration repair."
        ),
    ),
    PromptFailureCode.MODEL_RETRY_SCHEDULED: PromptFailureDefinition(
        diagnosis=(
            "Model transaction {operation} failed on attempt {attempt} of "
            "{max_attempts}: {error_message}"
        ),
        repair_requirement=(
            "Include all prior failure diagnostics in the next prompt, wait for the "
            "configured backoff, and retry the complete request and validation "
            "transaction."
        ),
    ),
    PromptFailureCode.PROVIDER_REQUEST_LIMIT_EXCEEDED: PromptFailureDefinition(
        diagnosis=(
            "Provider request {operation} exceeds a deterministic payload limit: "
            "{error_message}"
        ),
        repair_requirement=(
            "Do not retry the unchanged request. Reduce or pack the supplied media "
            "payload so it fits the provider limit, then submit it again."
        ),
    ),
    PromptFailureCode.PLANNERS_STAGE_ATTEMPT_INFEASIBLE: PromptFailureDefinition(
        diagnosis=(
            "ASTER attempt {attempt} became infeasible during {stage}: "
            "{error_message}"
        ),
        repair_requirement=(
            "Use the attached per-Slot diagnostics to change the failing source "
            "assignments or candidate choices, while preserving fixed Slots and all "
            "chronology, duration, visual-grounding, and capacity constraints."
        ),
    ),
    PromptFailureCode.RESPONSE_VALIDATION_FAILED: PromptFailureDefinition(
        diagnosis=(
            "The previous model response failed executable validation: "
            "{error_message}"
        ),
        repair_requirement=(
            "Correct the exact validation failure and return a response that conforms "
            "to the supplied response contract. Do not repeat the rejected value."
        ),
    ),
    PromptFailureCode.CANDIDATE_RETRIEVAL_FAILED: PromptFailureDefinition(
        diagnosis=(
            "Candidate retrieval for {slot_id} failed before a valid candidate set "
            "could be accepted: {error_message}"
        ),
        repair_requirement=(
            "Return the required number of valid, fixed-duration candidate timestamps "
            "inside the supplied source Segments. Candidate alternatives may overlap "
            "but must not duplicate an excluded timestamp exactly."
        ),
    ),
    PromptFailureCode.SOURCE_SEGMENTS_TOO_SHORT: (
        PromptFailureDefinition(
            diagnosis=(
                "The longest available source Segment for {slot_id} is "
                "{longest_segment_duration_sec} seconds, which is not longer than the "
                "planned clip duration of {planned_duration_sec} seconds."
            ),
            repair_requirement=(
                "Assign a different source Segment whose duration is longer than "
                "{planned_duration_sec} seconds. Only one complete clip must fit; "
                "overlapping alternative candidate windows are allowed."
            ),
        )
    ),
    PromptFailureCode.VISUALLY_STATIC: PromptFailureDefinition(
        diagnosis=(
            "Candidate {candidate_id} at {timestamp} has kinetic energy "
            "{kinetic_energy}, which does not exceed the configured moving-picture "
            "threshold {static_threshold}; it is effectively static."
        ),
        repair_requirement=(
            "Do not reuse this timestamp. Select source evidence with clearly visible "
            "subject, camera, or environmental motion above the configured threshold."
        ),
    ),
    PromptFailureCode.REQUIRED_SUBJECT_NOT_VISUALLY_CONFIRMED: (
        PromptFailureDefinition(
            diagnosis=(
                "Candidate {candidate_id} at {timestamp} did not visually confirm all "
                "required subjects {required_visible_subjects}. The visual evidence "
                "was: {visual_evidence}"
            ),
            repair_requirement=(
                "Choose a different Segment and visible event where every subject that "
                "must appear in this one clip can be verified. If the source never "
                "shows those subjects together, reduce required_visible_subjects or "
                "split the narrative requirement across separate Slots."
            ),
        )
    ),
    PromptFailureCode.DUPLICATE_CANDIDATE_RANGE: PromptFailureDefinition(
        diagnosis=(
            "Candidate {candidate_id} exactly duplicates a previously accepted or "
            "rejected timestamp {timestamp} for {slot_id}."
        ),
        repair_requirement=(
            "Shift the candidate to a different timestamp while preserving the exact "
            "planned clip duration. Partial overlap with another alternative is allowed."
        ),
    ),
    PromptFailureCode.NO_CANDIDATE_PASSED_VISUAL_DIAGNOSTICS: (
        PromptFailureDefinition(
            diagnosis=(
                "Every retrieved candidate for {slot_id} was rejected by deterministic "
                "motion checks or visual grounding. Candidate-level diagnostics are "
                "included in candidate_rejections."
            ),
            repair_requirement=(
                "Redesign the Slot's visible event, required_visible_subjects, and "
                "source_segment_ids. Move to different source evidence instead of "
                "paraphrasing the same unsupported request."
            ),
        )
    ),
    PromptFailureCode.INSUFFICIENT_VISUALLY_GROUNDED_CANDIDATES: (
        PromptFailureDefinition(
            diagnosis=(
                "Candidate retrieval ended with fewer visually grounded candidates "
                "than required. The per-Slot deficits are {shortages}."
            ),
            repair_requirement=(
                "Redesign every deficient Slot with different source evidence and at "
                "least one Segment longer than the planned clip, then retrieve and "
                "visually validate the missing candidates."
            ),
        )
    ),
    PromptFailureCode.SOURCE_CHRONOLOGY_OR_OVERLAP: PromptFailureDefinition(
        diagnosis=(
            "The proposed edit causes source chronology to move backward or overlap "
            "between {previous_slot_id} and {slot_id}."
        ),
        repair_requirement=(
            "Keep the original source ranges strictly increasing and non-overlapping. "
            "Replace only patches that preserve this invariant."
        ),
    ),
    PromptFailureCode.PATCH_DEGRADES_OR_REQUIRES_UNSCORED_PATH: (
        PromptFailureDefinition(
            diagnosis=(
                "The proposed replacement for {slot_id} lowers the weighted full-path "
                "score or requires a hard-cut pair that has not been visually scored."
            ),
            repair_requirement=(
                "Keep the current candidate or choose a replacement whose complete "
                "adjacent hard-cut path is scored and does not reduce the baseline."
            ),
        )
    ),
    PromptFailureCode.PATCH_EXCLUDED_BY_MAXIMAL_FEASIBLE_SUBSET: (
        PromptFailureDefinition(
            diagnosis=(
                "The proposed replacement for {slot_id} is individually plausible but "
                "cannot coexist with the higher-scoring maximal feasible patch subset."
            ),
            repair_requirement=(
                "Keep the accepted subset and omit this replacement unless a jointly "
                "feasible, non-overlapping, non-degrading alternative is available."
            ),
        )
    ),
    PromptFailureCode.PROVIDER_IMAGE_INSPECTION_FAILED: PromptFailureDefinition(
        diagnosis=(
            "The vision provider rejected image input for {operation} during content "
            "inspection: {error_message}"
        ),
        repair_requirement=(
            "Isolate the rejected candidate, resample a single representative frame, "
            "and continue without treating the rejected image batch as visual evidence."
        ),
    ),
    PromptFailureCode.PROVIDER_DATA_INSPECTION_FAILED: PromptFailureDefinition(
        diagnosis=(
            "The vision provider rejected {missing_shot_count} Shot annotations during "
            "content inspection, so those Shots have no reliable visual description."
        ),
        repair_requirement=(
            "Continue using successfully annotated Shots. Do not treat missing visual "
            "annotations as evidence for a person, action, location, or object."
        ),
    ),
}


def _template_values(details: dict[str, Any]) -> dict[str, str]:
    return {
        key: (
            json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            if isinstance(value, (dict, list, tuple))
            else str(value)
        )
        for key, value in details.items()
    }


def build_prompt_failure(
    reason_code: PromptFailureCode | str,
    **details: Any,
) -> dict[str, Any]:
    code = PromptFailureCode(reason_code)
    definition = PROMPT_FAILURE_CATALOG[code]
    values = _template_values(details)
    try:
        diagnosis = definition.diagnosis.format_map(values)
        repair_requirement = definition.repair_requirement.format_map(values)
    except KeyError as exc:
        raise ValueError(
            f"Missing detail {exc.args[0]!r} for prompt failure {code.value}"
        ) from exc
    return {
        "reason_code": code.value,
        "diagnosis": diagnosis,
        "repair_requirement": repair_requirement,
        **details,
    }


__all__ = [
    "PROMPT_FAILURE_CATALOG",
    "PromptFailureCode",
    "PromptFailureDefinition",
    "build_prompt_failure",
]
