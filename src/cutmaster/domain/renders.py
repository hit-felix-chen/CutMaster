"""Render Variant state names."""

from enum import StrEnum


class RenderVariantStatus(StrEnum):
    QUEUED = "queued"
    RENDERING = "rendering"
    READY = "ready"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    UNAVAILABLE = "unavailable"


__all__ = ["RenderVariantStatus"]
