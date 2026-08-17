"""Read models for Execution Attempts, jobs, and durable events."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Any

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
    model_usage_summary: Mapping[str, Any] | None
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
class AttemptLogEntryView:
    cursor: int
    timestamp: str | None
    level: str
    component: str | None
    event: str | None
    fields: str
    message: str


@dataclass(frozen=True)
class AttemptLogPageView:
    path: str
    exists: bool
    entries: tuple[AttemptLogEntryView, ...]
    start_cursor: int
    end_cursor: int
    has_more_before: bool


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


@dataclass(frozen=True)
class EventBoundsView:
    first_event_id: int | None
    last_event_id: int | None

    def __post_init__(self) -> None:
        first = self.first_event_id
        last = self.last_event_id
        if (first is None) != (last is None):
            raise ValueError("Durable Event bounds must both be present or absent")
        if first is None:
            return
        assert last is not None
        if (
            not isinstance(first, int)
            or isinstance(first, bool)
            or not isinstance(last, int)
            or isinstance(last, bool)
            or first <= 0
            or last < first
        ):
            raise ValueError("Durable Event bounds are invalid")


@dataclass(frozen=True)
class EventPageView:
    items: tuple[EventView, ...]
    bounds: EventBoundsView


def attempt_view(value: Mapping[str, Any]) -> AttemptView:
    usage = value.get("model_usage_summary")
    if usage is not None and not isinstance(usage, Mapping):
        raise TypeError("Invalid Attempt model usage persistence result")
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
        model_usage_summary=(
            None if usage is None else MappingProxyType(dict(usage))
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
    "AttemptLogEntryView",
    "AttemptLogPageView",
    "AttemptView",
    "EventBoundsView",
    "EventPageView",
    "EventView",
    "JobSubmissionView",
    "JobView",
    "attempt_view",
    "job_view",
]
