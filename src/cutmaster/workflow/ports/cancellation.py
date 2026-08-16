"""Cooperative cancellation boundary for long Workflow operations."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class WorkflowCancelledError(RuntimeError):
    """Raised at a safe Workflow boundary after cancellation has won."""


@runtime_checkable
class CancellationToken(Protocol):
    def raise_if_cancelled(self) -> None:
        """Raise the configured cancellation exception when cancellation won."""


def raise_if_cancelled(token: CancellationToken | None) -> None:
    """Poll an optional token without requiring callers to allocate a no-op."""

    if token is not None:
        token.raise_if_cancelled()


__all__ = [
    "CancellationToken",
    "WorkflowCancelledError",
    "raise_if_cancelled",
]
