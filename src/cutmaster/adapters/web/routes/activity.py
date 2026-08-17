"""Durable Execution Attempt and event-log queries."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from cutmaster.adapters.web.dependencies import (
    ApplicationDependency,
    IdempotencyKey,
)
from cutmaster.adapters.web.event_stream import (
    durable_event_frames,
    public_event_view,
)
from cutmaster.adapters.web.presenters import (
    attempt_view,
    job_view,
)
from cutmaster.adapters.web.routes._execution import execution_with_log
from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import StopAttemptCommand
from cutmaster.application.jobs.views import AttemptView
from cutmaster.domain.attempts import AttemptStatus
from cutmaster.domain.ids import (
    AttemptId,
    MaterialId,
    RenderVariantId,
    RunId,
)
from cutmaster.infrastructure.persistence.sqlite import ManagedStateNotFound

router = APIRouter(tags=["activity"])
_MAX_EVENT_ID = 9_223_372_036_854_775_807


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
    has_more = bool(
        len(items) == limit
        and application.jobs.activity(
            owner_type=owner_type,
            owner_id=owner_id,
            statuses=tuple(status_filter or ()),
            limit=1,
            offset=offset + limit,
        )
    )
    return {
        "items": [_activity_item(application, item) for item in items],
        "limit": limit,
        "offset": offset,
        "has_more": has_more,
    }


def _activity_item(
    application: CutMasterApplication,
    attempt: AttemptView,
) -> dict[str, object]:
    result = execution_with_log(
        application,
        attempt,
        application.jobs.get_job_for_attempt(attempt.attempt_id),
    )
    result["navigation"] = _owner_navigation(application, attempt)
    return result


def _owner_navigation(
    application: CutMasterApplication,
    attempt: AttemptView,
) -> dict[str, object] | None:
    """Project an owner hierarchy without leaking browser-route strings."""

    try:
        if attempt.owner_type == "material":
            material = application.materials.get(MaterialId.parse(attempt.owner_id))
            if material is None:
                return None
            return {
                "type": "material",
                "material_type": material.material_type.value,
                "material_id": str(material.material_id),
                "material_name": material.name,
            }
        if attempt.owner_type == "run":
            run = application.runs.get(RunId.parse(attempt.owner_id))
            project = application.projects.get(run.project_id)
            return {
                "type": "run",
                "project_id": str(run.project_id),
                "project_name": project.name,
                "run_id": str(run.run_id),
                "run_sequence": run.sequence,
            }
        if attempt.owner_type == "render_variant":
            variant = application.renders.get(RenderVariantId.parse(attempt.owner_id))
            edit = application.runs.get_frozen_edit(variant.edit_id)
            run = application.runs.get(edit.run_id)
            project = application.projects.get(run.project_id)
            return {
                "type": "render_variant",
                "project_id": str(run.project_id),
                "project_name": project.name,
                "run_id": str(run.run_id),
                "run_sequence": run.sequence,
                "edit_id": str(edit.edit_id),
                "edit_version": edit.sequence,
                "render_variant_id": str(variant.render_variant_id),
                "audio_mode": str(variant.specification["audio_mode"]),
            }
    except (ManagedStateNotFound, ValueError):
        # Historical Attempts can outlive a deleted owner in older databases.
        # They remain truthful Activity records, but must not create a dead link.
        return None
    return None


@router.get("/events")
def events(
    application: ApplicationDependency,
    after_event_id: int = Query(default=0, ge=0, le=_MAX_EVENT_ID),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, object]:
    items = application.jobs.events(
        after_event_id=after_event_id,
        limit=limit,
    )
    return {
        "items": [public_event_view(item) for item in items],
        "after_event_id": after_event_id,
        "last_event_id": (after_event_id if not items else items[-1].event_id),
    }


@router.get("/events/stream", response_class=StreamingResponse)
async def event_stream(
    request: Request,
    application: ApplicationDependency,
    after_event_id: Annotated[
        int | None,
        Query(ge=0, le=_MAX_EVENT_ID),
    ] = None,
    last_event_id: Annotated[
        int | None,
        Header(alias="Last-Event-ID", ge=0, le=_MAX_EVENT_ID),
    ] = None,
) -> StreamingResponse:
    """Replay and tail the canonical SQLite Event log over SSE."""

    cursor = (
        last_event_id
        if last_event_id is not None
        else (after_event_id if after_event_id is not None else 0)
    )
    return StreamingResponse(
        durable_event_frames(
            application,
            request,
            after_event_id=cursor,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


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
