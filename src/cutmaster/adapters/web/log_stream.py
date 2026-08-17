"""Attempt-scoped SSE frames backed by append-only managed Job logs."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import Request

from cutmaster.adapters.web.presenters import attempt_log_entry_view
from cutmaster.application import CutMasterApplication
from cutmaster.domain.attempts import TERMINAL_ATTEMPT_STATUSES
from cutmaster.domain.ids import AttemptId


async def attempt_log_frames(
    application: CutMasterApplication,
    request: Request,
    *,
    attempt_id: AttemptId,
    after_cursor: int,
    poll_interval_sec: float = 0.25,
    heartbeat_interval_sec: float = 15.0,
) -> AsyncIterator[str]:
    """Replay complete lines after one byte cursor, then tail until disconnect."""

    cursor = after_cursor
    quiet_terminal_polls = 0
    heartbeat_elapsed = 0.0
    while not await request.is_disconnected():
        page = application.jobs.attempt_log_after(
            attempt_id,
            cursor=cursor,
            limit=200,
        )
        if page.start_cursor < cursor:
            cursor = page.start_cursor
            yield _frame(
                "log_reset",
                {"cursor": cursor, "reason": "log_replaced"},
                event_id=cursor,
            )
        if page.entries:
            quiet_terminal_polls = 0
            heartbeat_elapsed = 0.0
            for entry in page.entries:
                cursor = entry.cursor
                yield _frame(
                    "log_entry",
                    attempt_log_entry_view(entry),
                    event_id=cursor,
                )
            continue

        attempt = application.jobs.get_attempt(attempt_id)
        if attempt.status in TERMINAL_ATTEMPT_STATUSES:
            quiet_terminal_polls += 1
            if quiet_terminal_polls >= 4:
                yield _frame(
                    "log_end",
                    {"cursor": cursor, "status": attempt.status.value},
                    event_id=cursor,
                )
                return
        await asyncio.sleep(poll_interval_sec)
        heartbeat_elapsed += poll_interval_sec
        if heartbeat_elapsed >= heartbeat_interval_sec:
            heartbeat_elapsed = 0.0
            yield ": heartbeat\n\n"


def _frame(event: str, data: object, *, event_id: int) -> str:
    return (
        f"id: {event_id}\n"
        f"event: {event}\n"
        f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


__all__ = ["attempt_log_frames"]
