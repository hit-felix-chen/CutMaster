"""ASTER Run state names."""

from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    PLANNERS = "planners"
    COMPLETE = "complete"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


__all__ = ["RunStatus"]
