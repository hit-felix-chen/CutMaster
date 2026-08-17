"""Attempt-scoped persisted and live Job log transport."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from cutmaster.adapters.web.dependencies import ApplicationDependency
from cutmaster.adapters.web.log_stream import attempt_log_frames
from cutmaster.adapters.web.presenters import attempt_log_page_view
from cutmaster.domain.ids import AttemptId

router = APIRouter(prefix="/attempts", tags=["logs"])
_MAX_CURSOR = 9_223_372_036_854_775_807


@router.get("/{attempt_id}/logs")
def attempt_logs(
    attempt_id: str,
    application: ApplicationDependency,
) -> dict[str, object]:
    """Return the last 50 complete lines and the exact absolute log path."""

    parsed = AttemptId.parse(attempt_id)
    result = attempt_log_page_view(application.jobs.attempt_log(parsed, tail=50))
    result["attempt_id"] = str(parsed)
    return result


@router.get("/{attempt_id}/logs/stream", response_class=StreamingResponse)
async def attempt_logs_stream(
    attempt_id: str,
    request: Request,
    application: ApplicationDependency,
    after_cursor: Annotated[
        int | None,
        Query(ge=0, le=_MAX_CURSOR),
    ] = None,
    last_event_id: Annotated[
        int | None,
        Header(alias="Last-Event-ID", ge=0, le=_MAX_CURSOR),
    ] = None,
) -> StreamingResponse:
    parsed = AttemptId.parse(attempt_id)
    application.jobs.get_attempt(parsed)
    cursor = (
        last_event_id
        if last_event_id is not None
        else (after_cursor if after_cursor is not None else 0)
    )
    return StreamingResponse(
        attempt_log_frames(
            application,
            request,
            attempt_id=parsed,
            after_cursor=cursor,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = ["router"]
