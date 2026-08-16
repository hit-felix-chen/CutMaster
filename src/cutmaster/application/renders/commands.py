"""Transport-neutral Render Variant commands."""

from __future__ import annotations

from dataclasses import dataclass

from cutmaster.domain.ids import AttemptId, FrozenEditId, RenderVariantId
from cutmaster.workflow.contracts.rendering import AudioMode


@dataclass(frozen=True)
class CreateRenderVariantCommand:
    command_id: str
    edit_id: FrozenEditId
    audio_mode: AudioMode

    def __post_init__(self) -> None:
        if not isinstance(self.audio_mode, str):
            raise TypeError("audio_mode must be a string")
        if self.audio_mode not in {"dialogue", "bgm_only"}:
            raise ValueError(f"Unsupported audio_mode: {self.audio_mode!r}")


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
