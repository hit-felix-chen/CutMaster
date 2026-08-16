from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

import cutmaster.adapters.web.routes.activity as activity_routes
from cutmaster.adapters.web.event_stream import durable_event_frames
from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import (
    ClaimJobCommand,
    EnqueueMaterialAnalysisCommand,
    FailAttemptCommand,
    JobsService,
)
from cutmaster.application.projects import CreateProjectCommand


class _DisconnectProbe:
    def __init__(self) -> None:
        self.disconnected = False

    async def is_disconnected(self) -> bool:
        return self.disconnected


def _command_id() -> str:
    return str(uuid4())


def _create_project(application: CutMasterApplication, name: str) -> None:
    application.projects.create(CreateProjectCommand(_command_id(), name))


def _decode_frame(frame: str) -> tuple[int | None, str | None, object | None]:
    event_id = None
    event_name = None
    data = None
    for line in frame.rstrip("\n").splitlines():
        if line.startswith("id: "):
            event_id = int(line.removeprefix("id: "))
        elif line.startswith("event: "):
            event_name = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data = json.loads(line.removeprefix("data: "))
    return event_id, event_name, data


def test_application_event_bounds_and_pages_are_ordered(
    application: CutMasterApplication,
) -> None:
    for index in range(5):
        _create_project(application, f"Page {index}")
    all_events = application.jobs.events(limit=100)

    page = application.jobs.event_page(
        after_event_id=all_events[0].event_id,
        limit=2,
    )

    assert application.jobs.event_bounds() == page.bounds
    assert [item.event_id for item in page.items] == [
        all_events[1].event_id,
        all_events[2].event_id,
    ]
    assert page.bounds.first_event_id == all_events[0].event_id
    assert page.bounds.last_event_id == all_events[-1].event_id


def test_stream_replays_multiple_pages_in_strict_order(
    application: CutMasterApplication,
) -> None:
    for index in range(6):
        _create_project(application, f"Replay {index}")
    events = application.jobs.events(limit=100)

    async def scenario() -> list[str]:
        stream = durable_event_frames(
            application,
            _DisconnectProbe(),
            after_event_id=events[0].event_id,
            page_size=2,
            poll_interval_sec=0.01,
            heartbeat_interval_sec=60,
        )
        try:
            return [await anext(stream) for _ in range(5)]
        finally:
            await stream.aclose()

    frames = asyncio.run(scenario())
    decoded = [_decode_frame(frame) for frame in frames]
    assert [item[0] for item in decoded] == [event.event_id for event in events[1:]]
    assert {item[1] for item in decoded} == {"durable_event"}
    assert [item[2]["event_id"] for item in decoded] == [
        event.event_id for event in events[1:]
    ]


def test_last_event_id_header_wins_and_response_has_sse_headers(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[int] = []

    async def finite_frames(_application, _request, *, after_event_id, **_kwargs):
        observed.append(after_event_id)
        yield ": finite\n\n"

    monkeypatch.setattr(activity_routes, "durable_event_frames", finite_frames)

    response = client.get(
        "/api/events/stream",
        params={"after_event_id": 4},
        headers={"Last-Event-ID": "9"},
    )

    assert response.status_code == 200
    assert observed == [9]
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    preflight = client.options(
        "/api/events/stream",
        headers={
            "Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Last-Event-ID",
        },
    )
    assert preflight.status_code == 200
    assert "last-event-id" in preflight.headers["access-control-allow-headers"].lower()
    assert (
        client.get(
            "/api/events/stream",
            headers={"Last-Event-ID": "not-an-event-id"},
        ).status_code
        == 422
    )


def test_empty_log_heartbeats_without_inventing_an_event(
    application: CutMasterApplication,
) -> None:
    async def scenario() -> str:
        stream = durable_event_frames(
            application,
            _DisconnectProbe(),
            after_event_id=0,
            poll_interval_sec=0.001,
            heartbeat_interval_sec=0.005,
        )
        try:
            return await asyncio.wait_for(anext(stream), timeout=1)
        finally:
            await stream.aclose()

    frame = asyncio.run(scenario())
    assert frame == ": heartbeat\n\n"
    assert application.jobs.event_bounds().first_event_id is None


def test_cursor_ahead_sends_resync_then_tails_new_writes(
    application: CutMasterApplication,
) -> None:
    _create_project(application, "Before reset")
    current = application.jobs.events()[0]

    async def scenario() -> tuple[str, str]:
        stream = durable_event_frames(
            application,
            _DisconnectProbe(),
            after_event_id=current.event_id + 100,
            poll_interval_sec=0.001,
            heartbeat_interval_sec=60,
        )
        try:
            resync = await anext(stream)
            await asyncio.to_thread(_create_project, application, "After reset")
            following = await asyncio.wait_for(anext(stream), timeout=1)
            return resync, following
        finally:
            await stream.aclose()

    resync, following = asyncio.run(scenario())
    reset_id, reset_name, reset_data = _decode_frame(resync)
    following_id, following_name, _following_data = _decode_frame(following)
    assert reset_name == "resync_required"
    assert reset_id == current.event_id
    assert reset_data == {
        "action": "refetch_authoritative_state",
        "available_first_event_id": current.event_id,
        "available_last_event_id": current.event_id,
        "reason": "cursor_ahead",
        "requested_after_event_id": current.event_id + 100,
        "schema_version": "1.0",
    }
    assert following_name == "durable_event"
    assert following_id == current.event_id + 1


def test_empty_reset_and_expired_cursor_are_explicit_resyncs(
    application: CutMasterApplication,
) -> None:
    async def empty_reset() -> str:
        stream = durable_event_frames(
            application,
            _DisconnectProbe(),
            after_event_id=7,
            poll_interval_sec=0.01,
            heartbeat_interval_sec=60,
        )
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    empty_frame = asyncio.run(empty_reset())
    assert _decode_frame(empty_frame) == (
        0,
        "resync_required",
        {
            "action": "refetch_authoritative_state",
            "available_first_event_id": None,
            "available_last_event_id": None,
            "reason": "cursor_ahead",
            "requested_after_event_id": 7,
            "schema_version": "1.0",
        },
    )

    for index in range(3):
        _create_project(application, f"Retained {index}")
    events = application.jobs.events(limit=10)
    database = application.settings.effective_configuration.data_root / "cutmaster.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "DELETE FROM events WHERE event_id < ?",
            (events[-1].event_id,),
        )

    async def expired() -> str:
        stream = durable_event_frames(
            application,
            _DisconnectProbe(),
            after_event_id=0,
            poll_interval_sec=0.01,
            heartbeat_interval_sec=60,
        )
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    expired_id, expired_name, expired_data = _decode_frame(asyncio.run(expired()))
    assert expired_name == "resync_required"
    assert expired_id == events[-1].event_id
    assert expired_data["reason"] == "cursor_expired"
    assert expired_data["available_first_event_id"] == events[-1].event_id


def test_disconnect_stops_polling_and_closes_the_generator(
    application: CutMasterApplication,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _create_project(application, "Disconnect")
    calls = 0
    original = JobsService.event_page

    def counted(self, **kwargs):
        nonlocal calls
        calls += 1
        return original(self, **kwargs)

    monkeypatch.setattr(JobsService, "event_page", counted)

    async def scenario() -> None:
        probe = _DisconnectProbe()
        stream = durable_event_frames(
            application,
            probe,
            after_event_id=0,
            poll_interval_sec=0.001,
            heartbeat_interval_sec=60,
        )
        assert _decode_frame(await anext(stream))[1] == "durable_event"
        probe.disconnected = True
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        await stream.aclose()
        await asyncio.sleep(0.01)

    asyncio.run(scenario())
    assert calls == 1


def test_stream_observes_a_concurrent_sqlite_writer(
    application: CutMasterApplication,
) -> None:
    async def scenario() -> str:
        stream = durable_event_frames(
            application,
            _DisconnectProbe(),
            after_event_id=0,
            poll_interval_sec=0.001,
            heartbeat_interval_sec=60,
        )
        pending = asyncio.create_task(anext(stream))
        try:
            await asyncio.sleep(0.01)
            await asyncio.to_thread(_create_project, application, "Concurrent")
            return await asyncio.wait_for(pending, timeout=1)
        finally:
            if not pending.done():
                pending.cancel()
            await stream.aclose()

    event_id, event_name, data = _decode_frame(asyncio.run(scenario()))
    assert event_id == 1
    assert event_name == "durable_event"
    assert data["event_type"] == "project.created"


def test_public_stream_never_carries_failure_provider_or_path_payloads(
    application: CutMasterApplication,
    tmp_path: Path,
    client: TestClient,
) -> None:
    source = tmp_path / "unsafe.mp4"
    source.write_bytes(b"video")
    material = application.materials.add(source, "video", "Unsafe Event")
    submission = application.jobs.enqueue_material_analysis(
        EnqueueMaterialAnalysisCommand(_command_id(), material.material_id)
    )
    assert (
        application.jobs.claim_next(
            ClaimJobCommand("unsafe-worker", 919, submission.job.job_id)
        )
        is not None
    )
    unsafe = "provider sk-secret failed at /Users/private/source.mp4"
    application.jobs.mark_failed(
        FailAttemptCommand(submission.attempt.attempt_id, unsafe)
    )
    failed = next(
        event
        for event in application.jobs.events(limit=20)
        if event.event_type == "attempt.failed"
    )

    async def scenario() -> str:
        stream = durable_event_frames(
            application,
            _DisconnectProbe(),
            after_event_id=failed.event_id - 1,
            poll_interval_sec=0.01,
            heartbeat_interval_sec=60,
        )
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    frame = asyncio.run(scenario())
    _event_id, event_name, data = _decode_frame(frame)
    assert event_name == "durable_event"
    assert data["payload"] == {}
    assert "sk-secret" not in frame
    assert "/Users/private" not in frame
    assert "provider" not in frame
    public_history = client.get(
        "/api/events",
        params={"after_event_id": failed.event_id - 1},
    )
    assert public_history.status_code == 200
    assert public_history.json()["items"][0]["payload"] == {}
    assert unsafe not in public_history.text
