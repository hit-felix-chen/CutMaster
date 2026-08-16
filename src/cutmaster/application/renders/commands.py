"""Transport-neutral Render Variant commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from cutmaster.domain.ids import AttemptId, FrozenEditId, RenderVariantId


@dataclass(frozen=True)
class CreateRenderVariantCommand:
    command_id: str
    edit_id: FrozenEditId
    specification: Mapping[str, Any]


@dataclass(frozen=True)
class CompleteRenderVariantCommand:
    command_id: str
    render_variant_id: RenderVariantId
    attempt_id: AttemptId
    master_relative_path: str
    frame_count: int
    duration_sec: float


@dataclass(frozen=True)
class RecoverRenderVariantCommand:
    command_id: str
    render_variant_id: RenderVariantId


@dataclass(frozen=True)
class VerifyRenderVariantCommand:
    command_id: str
    render_variant_id: RenderVariantId


@dataclass(frozen=True)
class DeleteRenderVariantCommand:
    command_id: str
    render_variant_id: RenderVariantId


__all__ = [
    "CompleteRenderVariantCommand",
    "CreateRenderVariantCommand",
    "DeleteRenderVariantCommand",
    "RecoverRenderVariantCommand",
    "VerifyRenderVariantCommand",
]

