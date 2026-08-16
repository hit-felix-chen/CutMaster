"""Cooperative cancellation boundary for long Workflow operations."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CancellationToken(Protocol):
    def raise_if_cancelled(self) -> None:
        """Raise the configured cancellation exception when cancellation won."""


__all__ = ["CancellationToken"]
