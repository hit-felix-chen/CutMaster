"""Operational event boundary used by Workflow stages."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class WorkflowEventSink(Protocol):
    def emit(
        self,
        *,
        component: str,
        name: str,
        message: str,
        attributes: Mapping[str, object] | None = None,
    ) -> None:
        """Publish one transport- and logger-neutral operational event."""


__all__ = ["WorkflowEventSink"]
