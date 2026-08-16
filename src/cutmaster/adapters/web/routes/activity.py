"""Durable Execution Attempt and event-log queries."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query

from cutmaster.adapters.web.dependencies import (
    ApplicationDependency,
    IdempotencyKey,
)
from cutmaster.adapters.web.presenters import (
    attempt_view,
    event_view,
    execution_view,
    job_view,
)
from cutmaster.application.jobs import StopAttemptCommand
from cutmaster.domain.attempts import AttemptStatus
from cutmaster.domain.ids import AttemptId


router = APIRouter(tags=["activity"])


@router.get("/activity")
def activity(
    application: ApplicationDependency,
    owner_type: str | None = None,
    owner_id: str | None = None,
    status_filter: Annotated[
        list[AttemptStatus] | None,
        Query(alias="status"),
    ] = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> dict[str, object]:
    items = application.jobs.activity(
        owner_type=owner_type,
        owner_id=owner_id,
        statuses=tuple(status_filter or ()),
        limit=limit,
        offset=offset,
    )
    return {
        "items": [
            execution_view(
                item,
                application.jobs.get_job_for_attempt(item.attempt_id),
            )
            for item in items
        ],
        "limit": limit,
        "offset": offset,
    }


@router.get("/events")
def events(
    application: ApplicationDependency,
    after_event_id: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, object]:
    items = application.jobs.events(
        after_event_id=after_event_id,
        limit=limit,
    )
    return {
        "items": [event_view(item) for item in items],
        "after_event_id": after_event_id,
        "last_event_id": (
            after_event_id if not items else items[-1].event_id
        ),
    }


@router.post("/attempts/{attempt_id}/stop")
def stop_attempt(
    attempt_id: str,
    application: ApplicationDependency,
    command_id: IdempotencyKey,
) -> dict[str, object]:
    result = application.jobs.stop(
        StopAttemptCommand(command_id, AttemptId.parse(attempt_id))
    )
    return {
        "attempt": attempt_view(result.attempt),
        "job": job_view(result.job),
    }


__all__ = ["router"]
