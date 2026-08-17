"""Application boundary for waking durable managed jobs."""

from __future__ import annotations

from typing import Protocol


class JobDispatcher(Protocol):
    """Wake the execution adapter for one durable Application submission."""

    def dispatch(self, submission: object) -> None: ...


class LifecycleJobSupervisor(JobDispatcher, Protocol):
    """A dispatcher whose transport lifecycle is owned by the inbound host."""

    def start(self) -> None: ...

    def stop(self, *, timeout_sec: float = 5.0) -> None: ...


__all__ = ["JobDispatcher", "LifecycleJobSupervisor"]
