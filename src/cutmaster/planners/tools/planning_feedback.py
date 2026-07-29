from __future__ import annotations

from typing import Any


def merge_planning_feedback(
    previous: dict[str, Any] | None,
    *,
    attempt: int,
    error: str,
    diagnostics: dict[str, Any],
    failed_slots: list[dict[str, Any]],
    candidates_per_slot: int,
) -> dict[str, Any]:
    previous = previous or {}
    accumulated: dict[tuple[str, ...], dict[str, Any]] = {}
    unassigned: list[dict[str, Any]] = []
    for failed in [*(previous.get("failed_slots") or []), *failed_slots]:
        segment_ids = tuple(
            str(value) for value in failed.get("source_segment_ids") or []
        )
        if segment_ids:
            accumulated[segment_ids] = failed
        else:
            unassigned.append(failed)

    forbidden_segment_ids = {
        str(value) for value in previous.get("forbidden_segment_ids") or []
    }
    forbidden_segment_ids.update(
        segment_id
        for failed in failed_slots
        if failed.get("missing_candidates") == candidates_per_slot
        for segment_id in failed.get("source_segment_ids") or []
    )
    failure_history = list(previous.get("failure_history") or [])
    failure_history.append(
        {
            "attempt": attempt,
            "error": error,
            "diagnostics": diagnostics,
            "failed_slots": failed_slots,
        }
    )
    return {
        "attempt": attempt,
        "error": error,
        "diagnostics": diagnostics,
        "current_failed_slots": failed_slots,
        "failed_slots": [*accumulated.values(), *unassigned],
        "forbidden_segment_ids": sorted(forbidden_segment_ids),
        "failure_history": failure_history,
        "instruction": (
            "Replan with supported source Segments in source order. Avoid every Segment "
            "assignment accumulated across earlier candidate shortages or empty "
            "chronological paths."
        ),
    }


__all__ = ["merge_planning_feedback"]

