"""Opaque identifiers shared by CutMaster domain objects."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Self
from uuid import RFC_4122, UUID, uuid4


@dataclass(frozen=True, order=True)
class EntityId:
    value: str
    prefix: ClassVar[str] = "id"

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError(f"{type(self).__name__} value must be a string")
        expected = f"{self.prefix}_"
        if not self.value.startswith(expected):
            raise ValueError(f"{type(self).__name__} must start with {expected!r}")
        raw_uuid = self.value[len(expected) :]
        try:
            parsed = UUID(raw_uuid)
        except ValueError as exc:
            raise ValueError(f"Invalid {type(self).__name__}: {self.value!r}") from exc
        if (
            str(parsed) != raw_uuid
            or parsed.version != 4
            or parsed.variant != RFC_4122
        ):
            raise ValueError(
                f"{type(self).__name__} must contain a canonical lowercase UUIDv4"
            )

    @classmethod
    def new(cls) -> Self:
        return cls(f"{cls.prefix}_{uuid4()}")

    @classmethod
    def parse(cls, value: str) -> Self:
        return cls(value)

    def __str__(self) -> str:
        return self.value


class MaterialId(EntityId):
    prefix = "mat"


class ProjectId(EntityId):
    prefix = "project"


class RunId(EntityId):
    prefix = "run"


class FrozenEditId(EntityId):
    prefix = "edit"


class RenderVariantId(EntityId):
    prefix = "render"


class AttemptId(EntityId):
    prefix = "attempt"


class JobId(EntityId):
    prefix = "job"


class NotificationId(EntityId):
    prefix = "notification"


__all__ = [
    "AttemptId",
    "EntityId",
    "FrozenEditId",
    "JobId",
    "MaterialId",
    "NotificationId",
    "ProjectId",
    "RenderVariantId",
    "RunId",
]
