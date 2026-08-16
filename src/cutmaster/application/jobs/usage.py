"""Strict, privacy-safe model usage summaries for managed Attempts."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

USAGE_CURRENCY = "CNY"
USAGE_PRICE_UNIT = "yuan_per_million_tokens"
TOKEN_USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_prompt_tokens",
    "uncached_prompt_tokens",
    "reasoning_tokens",
)
COST_USAGE_FIELDS = (
    "uncached_input_cost_yuan",
    "cached_input_cost_yuan",
    "output_cost_yuan",
    "total_cost_yuan",
)
USAGE_COUNT_FIELDS = (
    "request_count",
    "reported_usage_count",
    "unreported_usage_count",
    "priced_usage_count",
    "unpriced_usage_count",
)


def normalize_usage_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a summary and rebuild only the public aggregate schema."""

    if not isinstance(value, Mapping):
        raise TypeError("model usage summary must be a mapping")
    if value.get("currency") != USAGE_CURRENCY:
        raise ValueError("model usage currency must be CNY")
    if value.get("price_unit") != USAGE_PRICE_UNIT:
        raise ValueError(
            "model usage price_unit must be yuan_per_million_tokens"
        )
    _validate_bucket(value, "model usage summary")
    for group_name in ("by_model", "by_task"):
        group = value.get(group_name)
        if not isinstance(group, Mapping):
            raise TypeError(f"model usage {group_name} must be a mapping")
        for key, bucket in group.items():
            _validate_group_key(key, group_name)
            if not isinstance(bucket, Mapping):
                raise TypeError(f"model usage {group_name} buckets must be mappings")
            _validate_bucket(bucket, f"model usage {group_name} bucket")
    # merge_usage_summaries is the canonical aggregation/rounding path and
    # intentionally strips raw calls, provider payloads, IDs, and artifacts.
    from cutmaster.infrastructure.models.openai_compatible import (
        merge_usage_summaries,
    )

    return merge_usage_summaries([dict(value)])


def compact_usage_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    normalized = normalize_usage_summary(value)
    normalized.pop("by_model")
    normalized.pop("by_task")
    return normalized


def _validate_bucket(value: Mapping[str, Any], label: str) -> None:
    for field_name in (*USAGE_COUNT_FIELDS, *TOKEN_USAGE_FIELDS):
        field_value = value.get(field_name)
        if (
            not isinstance(field_value, int)
            or isinstance(field_value, bool)
            or field_value < 0
        ):
            raise ValueError(f"{label} {field_name} must be a non-negative integer")
    for field_name in COST_USAGE_FIELDS:
        field_value = value.get(field_name)
        if (
            not isinstance(field_value, (int, float))
            or isinstance(field_value, bool)
            or not math.isfinite(float(field_value))
            or float(field_value) < 0
        ):
            raise ValueError(f"{label} {field_name} must be finite and non-negative")
    if (
        int(value["reported_usage_count"])
        + int(value["unreported_usage_count"])
        != int(value["request_count"])
    ):
        raise ValueError(f"{label} reported request counts are inconsistent")
    if (
        int(value["priced_usage_count"])
        + int(value["unpriced_usage_count"])
        != int(value["reported_usage_count"])
    ):
        raise ValueError(f"{label} priced request counts are inconsistent")


def _validate_group_key(value: object, group_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"model usage {group_name} keys must be non-empty strings")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"model usage {group_name} keys contain control characters")


__all__ = [
    "USAGE_CURRENCY",
    "USAGE_PRICE_UNIT",
    "compact_usage_summary",
    "normalize_usage_summary",
]
