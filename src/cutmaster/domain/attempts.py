"""Execution Attempt states shared across managed operations."""

from enum import StrEnum


class AttemptStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRYING = "retrying"
    STOPPING = "stopping"
    INTERRUPTED = "interrupted"
    COMPLETE = "complete"
    FAILED = "failed"


TERMINAL_ATTEMPT_STATUSES = frozenset(
    {AttemptStatus.INTERRUPTED, AttemptStatus.COMPLETE, AttemptStatus.FAILED}
)


__all__ = ["AttemptStatus", "TERMINAL_ATTEMPT_STATUSES"]
