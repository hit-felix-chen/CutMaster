"""SQLite-backed Server-Sent Events without an in-process event authority."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from time import monotonic
from typing import Protocol

from cutmaster.adapters.web.presenters import event_view
from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import EventBoundsView, EventView


class DisconnectProbe(Protocol):
    async def is_disconnected(self) -> bool: ...


Sleep = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]


async def durable_event_frames(
    application: CutMasterApplication,
    request: DisconnectProbe,
    *,
    after_event_id: int,
    page_size: int = 200,
    poll_interval_sec: float = 0.25,
    heartbeat_interval_sec: float = 15.0,
    sleeper: Sleep = asyncio.sleep,
    clock: Clock = monotonic,
) -> AsyncIterator[str]:
    """Replay and tail public durable Events until the client disconnects."""

    _validate_stream_options(
        after_event_id,
        page_size,
        poll_interval_sec,
        heartbeat_interval_sec,
    )
    cursor = after_event_id
    last_emission = clock()
    while True:
        if await request.is_disconnected():
            return
        page = await asyncio.to_thread(
            application.jobs.event_page,
            after_event_id=cursor,
            limit=page_size,
        )
        reason = _resync_reason(cursor, page.bounds)
        if reason is not None:
            requested_cursor = cursor
            cursor = page.bounds.last_event_id or 0
            if await request.is_disconnected():
                return
            yield _resync_frame(
                cursor=cursor,
                requested_after_event_id=requested_cursor,
                bounds=page.bounds,
                reason=reason,
            )
            last_emission = clock()
            continue

        for event in page.items:
            if event.event_id <= cursor:
                raise RuntimeError("Durable Event page is not strictly ordered")
            if await request.is_disconnected():
                return
            yield _durable_event_frame(event)
            cursor = event.event_id
            last_emission = clock()
        if len(page.items) >= page_size:
            continue

        now = clock()
        if now - last_emission >= heartbeat_interval_sec:
            if await request.is_disconnected():
                return
            yield ": heartbeat\n\n"
            last_emission = clock()
        remaining_to_heartbeat = max(
            0.0,
            heartbeat_interval_sec - (clock() - last_emission),
        )
        await sleeper(min(poll_interval_sec, remaining_to_heartbeat))


def public_event_view(event: EventView) -> dict[str, object]:
    """Strip opaque worker/provider diagnostics from a public invalidation Event."""

    result = event_view(event)
    result["payload"] = {}
    return result


def _resync_reason(cursor: int, bounds: EventBoundsView) -> str | None:
    first = bounds.first_event_id
    last = bounds.last_event_id
    if (first is None) != (last is None):
        raise RuntimeError("Durable Event bounds are inconsistent")
    if last is None:
        return "cursor_ahead" if cursor > 0 else None
    assert first is not None
    if cursor > last:
        return "cursor_ahead"
    if cursor < first - 1:
        return "cursor_expired"
    return None


def _durable_event_frame(event: EventView) -> str:
    return _sse_frame(
        event_id=event.event_id,
        event_name="durable_event",
        data=public_event_view(event),
    )


def _resync_frame(
    *,
    cursor: int,
    requested_after_event_id: int,
    bounds: EventBoundsView,
    reason: str,
) -> str:
    return _sse_frame(
        event_id=cursor,
        event_name="resync_required",
        data={
            "schema_version": "1.0",
            "reason": reason,
            "requested_after_event_id": requested_after_event_id,
            "available_first_event_id": bounds.first_event_id,
            "available_last_event_id": bounds.last_event_id,
            "action": "refetch_authoritative_state",
        },
    )


def _sse_frame(
    *,
    event_id: int,
    event_name: str,
    data: dict[str, object],
) -> str:
    encoded = json.dumps(
        data,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return f"id: {event_id}\nevent: {event_name}\ndata: {encoded}\n\n"


def _validate_stream_options(
    after_event_id: int,
    page_size: int,
    poll_interval_sec: float,
    heartbeat_interval_sec: float,
) -> None:
    if (
        not isinstance(after_event_id, int)
        or isinstance(after_event_id, bool)
        or after_event_id < 0
        or after_event_id > 9_223_372_036_854_775_807
    ):
        raise ValueError("after_event_id must be a non-negative SQLite integer")
    if (
        not isinstance(page_size, int)
        or isinstance(page_size, bool)
        or not 1 <= page_size <= 1000
    ):
        raise ValueError("page_size must be between 1 and 1000")
    for value, label in (
        (poll_interval_sec, "poll_interval_sec"),
        (heartbeat_interval_sec, "heartbeat_interval_sec"),
    ):
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"{label} must be positive")


__all__ = [
    "DisconnectProbe",
    "durable_event_frames",
    "public_event_view",
]
