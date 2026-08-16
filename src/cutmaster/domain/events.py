"""Durable Application event envelope."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from math import isfinite
from types import MappingProxyType
from typing import TypeAlias


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = (
    JsonScalar | tuple["JsonValue", ...] | Mapping[str, "JsonValue"]
)


def _freeze_json(value: object, field_name: str) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError(f"{field_name} must not contain non-finite numbers")
        return value
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item, f"{field_name}[]") for item in value)
    if isinstance(value, Mapping):
        frozen: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{field_name} keys must be strings")
            frozen[key] = _freeze_json(item, f"{field_name}.{key}")
        return MappingProxyType(frozen)
    raise TypeError(f"{field_name} must contain only JSON-compatible values")


def _empty_payload() -> Mapping[str, JsonValue]:
    return MappingProxyType({})


def _thaw_json(value: JsonValue) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class DomainEvent:
    event_type: str
    occurred_at: datetime
    object_type: str
    object_id: str
    payload: Mapping[str, JsonValue] = field(default_factory=_empty_payload)
    schema_version: str = "1.0"

    def __post_init__(self) -> None:
        for field_name in ("event_type", "object_type", "object_id", "schema_version"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if not isinstance(self.occurred_at, datetime):
            raise TypeError("occurred_at must be a datetime")
        if self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        frozen_payload = _freeze_json(self.payload, "payload")
        if not isinstance(frozen_payload, Mapping):
            raise TypeError("payload must be a mapping")
        object.__setattr__(self, "payload", frozen_payload)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible durable representation."""

        return {
            "event_type": self.event_type,
            "occurred_at": self.occurred_at.isoformat(),
            "object_type": self.object_type,
            "object_id": self.object_id,
            "payload": _thaw_json(self.payload),
            "schema_version": self.schema_version,
        }


__all__ = ["DomainEvent"]
