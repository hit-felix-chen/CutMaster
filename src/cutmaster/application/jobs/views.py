"""Read models for Execution Attempts, jobs, and durable events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping

from cutmaster.domain.attempts import AttemptStatus
from cutmaster.domain.ids import AttemptId, JobId


@dataclass(frozen=True)
class AttemptView:
    attempt_id: AttemptId
    operation_type: str
    owner_type: str
    owner_id: str
    sequence: int
    status: AttemptStatus
    command_id: str
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime


@dataclass(frozen=True)
class JobView:
    job_id: JobId
    attempt_id: AttemptId
    status: AttemptStatus
    stop_requested: bool
    worker_id: str | None
    process_id: int | None
    heartbeat_at: datetime | None
    progress: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class JobSubmissionView:
    attempt: AttemptView
    job: JobView


@dataclass(frozen=True)
class EventView:
    event_id: int
    event_type: str
    occurred_at: datetime
    object_type: str
    object_id: str
    command_id: str | None
    attempt_id: AttemptId | None
    job_id: JobId | None
    payload: Mapping[str, Any]
    schema_version: str


def attempt_view(value: Mapping[str, Any]) -> AttemptView:
    return AttemptView(
        attempt_id=AttemptId.parse(str(value["attempt_id"])),
        operation_type=str(value["operation_type"]),
        owner_type=str(value["owner_type"]),
        owner_id=str(value["owner_id"]),
        sequence=int(value["sequence"]),
        status=AttemptStatus(str(value["status"])),
        command_id=str(value["command_id"]),
        error_message=(
            None if value.get("error_message") is None else str(value["error_message"])
        ),
        created_at=datetime.fromisoformat(str(value["created_at"])),
        started_at=(
            None
            if value.get("started_at") is None
            else datetime.fromisoformat(str(value["started_at"]))
        ),
        finished_at=(
            None
            if value.get("finished_at") is None
            else datetime.fromisoformat(str(value["finished_at"]))
        ),
        updated_at=datetime.fromisoformat(str(value["updated_at"])),
    )


def job_view(value: Mapping[str, Any]) -> JobView:
    progress = value.get("progress", {})
    if not isinstance(progress, Mapping):
        raise TypeError("Invalid job progress persistence result")
    return JobView(
        job_id=JobId.parse(str(value["job_id"])),
        attempt_id=AttemptId.parse(str(value["attempt_id"])),
        status=AttemptStatus(str(value["status"])),
        stop_requested=bool(value["stop_requested"]),
        worker_id=None if value.get("worker_id") is None else str(value["worker_id"]),
        process_id=(
            None if value.get("process_id") is None else int(value["process_id"])
        ),
        heartbeat_at=(
            None
            if value.get("heartbeat_at") is None
            else datetime.fromisoformat(str(value["heartbeat_at"]))
        ),
        progress=MappingProxyType(dict(progress)),
        created_at=datetime.fromisoformat(str(value["created_at"])),
        updated_at=datetime.fromisoformat(str(value["updated_at"])),
    )


__all__ = [
    "AttemptView",
    "EventView",
    "JobSubmissionView",
    "JobView",
    "attempt_view",
    "job_view",
]

