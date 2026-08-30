"""Transport-neutral commands for durable managed jobs."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from cutmaster.domain.ids import AttemptId, JobId, MaterialId


@dataclass(frozen=True)
class EnqueueMaterialAnalysisCommand:
    command_id: str
    material_id: MaterialId


@dataclass(frozen=True)
class RetryMaterialAnalysisCommand:
    command_id: str
    material_id: MaterialId


@dataclass(frozen=True)
class ResumeMaterialAnalysisCommand:
    command_id: str
    material_id: MaterialId


@dataclass(frozen=True)
class StopAttemptCommand:
    command_id: str
    attempt_id: AttemptId


@dataclass(frozen=True)
class DismissActivityAttemptsCommand:
    command_id: str
    attempt_ids: tuple[AttemptId, ...]


@dataclass(frozen=True)
class ClaimJobCommand:
    worker_id: str
    process_id: int
    job_id: JobId | None = None


@dataclass(frozen=True)
class ClaimSupervisedJobCommand:
    worker_id: str
    process_id: int
    max_active_jobs: int


@dataclass(frozen=True)
class AdoptSupervisedJobCommand:
    job_id: JobId
    attempt_id: AttemptId
    expected_worker_id: str
    expected_process_id: int
    worker_id: str
    process_id: int


@dataclass(frozen=True)
class HeartbeatJobCommand:
    job_id: JobId
    progress: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class FailAttemptCommand:
    attempt_id: AttemptId
    error_message: str


@dataclass(frozen=True)
class RecordAttemptUsageCommand:
    attempt_id: AttemptId
    model_usage_summary: Mapping[str, Any]


@dataclass(frozen=True)
class InterruptOrphansCommand:
    heartbeat_before: datetime


__all__ = [
    "AdoptSupervisedJobCommand",
    "ClaimJobCommand",
    "ClaimSupervisedJobCommand",
    "EnqueueMaterialAnalysisCommand",
    "DismissActivityAttemptsCommand",
    "FailAttemptCommand",
    "HeartbeatJobCommand",
    "InterruptOrphansCommand",
    "RecordAttemptUsageCommand",
    "ResumeMaterialAnalysisCommand",
    "RetryMaterialAnalysisCommand",
    "StopAttemptCommand",
]
