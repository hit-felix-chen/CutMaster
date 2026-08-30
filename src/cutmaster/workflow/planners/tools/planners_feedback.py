from __future__ import annotations

import json
from typing import Any


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if str(item)]


def _missing_candidate_count(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value >= 0 and value.is_integer():
        return int(value)
    return None


def is_hard_failed_slot_record(
    value: Any,
    *,
    candidates_per_slot: int,
) -> bool:
    """Return whether a Slot assignment is proven to have zero candidates.

    ``missing_candidates`` below the configured target proves that at least one
    candidate survived and therefore always wins over a stale hard marker.
    Legacy records are accepted only when their missing count reaches the full
    target.  Modern producers may persist an explicit ``hard_failure`` marker
    when the terminal ``failed_slot_ids`` contract proves an empty pool even if
    the compact record has no missing-count field.
    """

    if not isinstance(value, dict):
        return False
    missing_candidates = _missing_candidate_count(
        value.get("missing_candidates")
    )
    if missing_candidates is not None:
        if missing_candidates < candidates_per_slot:
            return False
        return True
    return value.get("hard_failure") is True


def _normalize_hard_failed_slot_record(
    value: Any,
    *,
    candidates_per_slot: int,
) -> dict[str, Any] | None:
    if not is_hard_failed_slot_record(
        value,
        candidates_per_slot=candidates_per_slot,
    ):
        return None
    return {**value, "hard_failure": True}


def _deduplicate_objects(values: list[Any]) -> list[Any]:
    """Preserve JSON diagnostics in arrival order without losing evidence."""

    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        identity = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        if identity in seen:
            continue
        seen.add(identity)
        result.append(value)
    return result


def _merge_failed_assignment(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> dict[str, Any]:
    merged = {**previous, **current}
    rejections = _deduplicate_objects(
        [
            *(previous.get("candidate_rejections") or []),
            *(current.get("candidate_rejections") or []),
        ]
    )
    if rejections:
        merged["candidate_rejections"] = rejections
    return merged


def _compact_failure_history_entry(
    *,
    attempt: int,
    error: str,
    diagnostics: dict[str, Any],
    failed_slots: list[dict[str, Any]],
) -> dict[str, Any]:
    """Keep audit chronology without duplicating prompt-heavy evidence."""

    failed_slot_ids = {
        str(item.get("slot_id") or "")
        for item in failed_slots
        if str(item.get("slot_id") or "")
    }
    entry: dict[str, Any] = {
        "attempt": attempt,
        "error": error,
        "failed_slot_ids": sorted(failed_slot_ids),
        "hard_failure_only": True,
    }
    reason_code = str(diagnostics.get("reason_code") or "")
    if reason_code:
        entry["reason_code"] = reason_code
    unavailable = sorted(
        set(_string_list(diagnostics.get("unavailable_source_segment_ids")))
    )
    if unavailable:
        entry["unavailable_source_segment_ids"] = unavailable
    return entry


def _compact_previous_failure_history_entry(
    value: Any,
    *,
    candidates_per_slot: int,
) -> dict[str, Any] | None:
    """Drop ambiguous legacy failure IDs while retaining compact audit data."""

    if not isinstance(value, dict):
        return None
    hard_slot_ids: set[str] = set()
    if value.get("hard_failure_only") is True:
        hard_slot_ids.update(_string_list(value.get("failed_slot_ids")))
    else:
        for failed in value.get("failed_slots") or []:
            normalized = _normalize_hard_failed_slot_record(
                failed,
                candidates_per_slot=candidates_per_slot,
            )
            if normalized is None:
                continue
            slot_id = str(normalized.get("slot_id") or "")
            if slot_id:
                hard_slot_ids.add(slot_id)
    compact: dict[str, Any] = {
        "attempt": value.get("attempt"),
        "error": str(value.get("error") or ""),
        "failed_slot_ids": sorted(hard_slot_ids),
        "hard_failure_only": True,
    }
    reason_code = str(value.get("reason_code") or "")
    if reason_code:
        compact["reason_code"] = reason_code
    unavailable = sorted(
        set(_string_list(value.get("unavailable_source_segment_ids")))
    )
    if unavailable:
        compact["unavailable_source_segment_ids"] = unavailable
    return compact


def compact_failure_history(
    value: Any,
    *,
    candidates_per_slot: int,
) -> list[dict[str, Any]]:
    """Normalize persisted history without carrying legacy partial failures."""

    if not isinstance(value, list):
        return []
    return [
        compact
        for item in value
        if (
            compact := _compact_previous_failure_history_entry(
                item,
                candidates_per_slot=candidates_per_slot,
            )
        )
        is not None
    ]


def merge_planners_feedback(
    previous: dict[str, Any] | None,
    *,
    attempt: int,
    error: str,
    diagnostics: dict[str, Any],
    failed_slots: list[dict[str, Any]],
    candidates_per_slot: int,
) -> dict[str, Any]:
    """Merge outer-ASTER failure evidence for the next full Arrangement call.

    Timeline's producer contract is carried inside ``diagnostics``:

    ``failed_slot_diagnostics``
        A list of failure snapshots.  Every snapshot identifies ``slot_id`` and
        the ``source_segment_ids`` assignment in effect when it failed, and may
        include ``reason_code`` plus complete ``candidate_rejections`` (including
        each rejection's reason and visual evidence).  The Planners service
        passes only terminal zero-candidate snapshots as ``failed_slots``. This
        function defensively rejects any current or checkpoint-restored partial
        shortage, then preserves each distinct hard-failed
        ``(slot_id, source_segment_ids)`` assignment.

    ``unavailable_source_segment_ids``
        Source Segments proven intrinsically unusable independent of Slot
        semantics.  Only this explicit set becomes the full Arrangement global
        blacklist, and it accumulates forever. Subject, semantic, relevance, or
        other zero-candidate failures remain Slot-specific assignments; partial
        shortages remain usable and are not recorded as failures.

    A partial shortage never forbids an assignment. Legacy diagnostics without
    explicit terminal fields become hard only when
    ``missing_candidates >= candidates_per_slot``.
    """

    previous = previous or {}
    accumulated: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for failed in [*(previous.get("failed_slots") or []), *failed_slots]:
        hard_failed = _normalize_hard_failed_slot_record(
            failed,
            candidates_per_slot=candidates_per_slot,
        )
        if hard_failed is None:
            continue
        slot_id = str(failed.get("slot_id") or "")
        segment_ids = tuple(
            _string_list(failed.get("source_segment_ids"))
        )
        normalized = {
            **hard_failed,
            "slot_id": slot_id,
            "source_segment_ids": list(segment_ids),
        }
        key = (slot_id, segment_ids)
        if key in accumulated:
            accumulated[key] = _merge_failed_assignment(
                accumulated[key],
                normalized,
            )
        else:
            accumulated[key] = normalized

    unavailable_source_segment_ids = {
        *_string_list(previous.get("unavailable_source_segment_ids")),
        *_string_list(diagnostics.get("unavailable_source_segment_ids")),
    }
    current_hard_failed_slots = [
        normalized
        for failed in failed_slots
        if (
            normalized := _normalize_hard_failed_slot_record(
                failed,
                candidates_per_slot=candidates_per_slot,
            )
        )
        is not None
    ]
    failure_history = compact_failure_history(
        previous.get("failure_history"),
        candidates_per_slot=candidates_per_slot,
    )
    failure_history.append(
        _compact_failure_history_entry(
            attempt=attempt,
            error=error,
            diagnostics=diagnostics,
            failed_slots=current_hard_failed_slots,
        )
    )
    unavailable = sorted(unavailable_source_segment_ids)
    return {
        "attempt": attempt,
        "error": error,
        "failed_slots": list(accumulated.values()),
        "unavailable_source_segment_ids": unavailable,
        "failure_history": failure_history,
    }


__all__ = [
    "compact_failure_history",
    "is_hard_failed_slot_record",
    "merge_planners_feedback",
]
