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
            _activity_item(application, item)
            for item in items
        ],
        "limit": limit,
        "offset": offset,
    }


def _activity_item(
    application: CutMasterApplication,
    attempt: AttemptView,
) -> dict[str, object]:
    result = execution_view(
        attempt,
        application.jobs.get_job_for_attempt(attempt.attempt_id),
    )
    result["navigation"] = _owner_navigation(application, attempt)
    return result


def _owner_navigation(
    application: CutMasterApplication,
    attempt: AttemptView,
) -> dict[str, str] | None:
    """Project an owner hierarchy without leaking browser-route strings."""

    if attempt.owner_type == "material":
        material = application.materials.get(MaterialId.parse(attempt.owner_id))
        if material is None:
            return None
        return {
            "type": "material",
            "material_type": material.material_type.value,
            "material_id": str(material.material_id),
        }
    if attempt.owner_type == "run":
        run = application.runs.get(RunId.parse(attempt.owner_id))
        return {
            "type": "run",
            "project_id": str(run.project_id),
            "run_id": str(run.run_id),
        }
    if attempt.owner_type == "render_variant":
        variant = application.renders.get(RenderVariantId.parse(attempt.owner_id))
        edit = application.runs.get_frozen_edit(variant.edit_id)
        run = application.runs.get(edit.run_id)
        return {
            "type": "render_variant",
            "project_id": str(run.project_id),
            "run_id": str(run.run_id),
            "edit_id": str(edit.edit_id),
            "render_variant_id": str(variant.render_variant_id),
        }
    return None


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
