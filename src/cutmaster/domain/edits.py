"""Frozen Edit identity values."""

from dataclasses import dataclass
from enum import StrEnum

from cutmaster.domain.ids import FrozenEditId, RunId


class FrozenEditOrigin(StrEnum):
    INITIAL = "initial"
    GUIDED_REVISION = "guided_revision"


@dataclass(frozen=True)
class FrozenEdit:
    edit_id: FrozenEditId
    run_id: RunId
    origin: FrozenEditOrigin
    parent_edit_id: FrozenEditId | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.edit_id, FrozenEditId):
            raise TypeError("edit_id must be a FrozenEditId")
        if not isinstance(self.run_id, RunId):
            raise TypeError("run_id must be a RunId")
        if not isinstance(self.origin, FrozenEditOrigin):
            raise TypeError("origin must be a FrozenEditOrigin")
        if self.parent_edit_id is not None and not isinstance(
            self.parent_edit_id,
            FrozenEditId,
        ):
            raise TypeError("parent_edit_id must be a FrozenEditId or None")
        if (
            self.origin is FrozenEditOrigin.INITIAL
            and self.parent_edit_id is not None
        ):
            raise ValueError("An initial Frozen Edit cannot have a parent")
        if (
            self.origin is FrozenEditOrigin.GUIDED_REVISION
            and self.parent_edit_id is None
        ):
            raise ValueError("A guided revision Frozen Edit requires a parent")
        if self.parent_edit_id == self.edit_id:
            raise ValueError("A Frozen Edit cannot be its own parent")


__all__ = ["FrozenEdit", "FrozenEditOrigin"]
