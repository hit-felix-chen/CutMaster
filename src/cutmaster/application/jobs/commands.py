"""Transport-neutral commands for durable managed jobs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Any

from cutmaster.domain.ids import AttemptId, JobId, MaterialId


@dataclass(frozen=True)
class EnqueueMaterialAnalysisCommand:
    command_id: str
    material_id: MaterialId


@dataclass(frozen=True)
class StopAttemptCommand:
    command_id: str
    attempt_id: AttemptId


@dataclass(frozen=True)
class ClaimJobCommand:
    worker_id: str
    process_id: int
    job_id: JobId | None = None


@dataclass(frozen=True)
class HeartbeatJobCommand:
    job_id: JobId
    progress: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class FailAttemptCommand:
    attempt_id: AttemptId
    error_message: str


@dataclass(frozen=True)
class InterruptOrphansCommand:
    heartbeat_before: datetime


__all__ = [
    "ClaimJobCommand",
    "EnqueueMaterialAnalysisCommand",
    "FailAttemptCommand",
    "HeartbeatJobCommand",
    "InterruptOrphansCommand",
    "StopAttemptCommand",
]
