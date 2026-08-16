from __future__ import annotations

from uuid import uuid4

from fastapi.testclient import TestClient


def key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


def test_activity_and_events_read_durable_state(client: TestClient) -> None:
    created = client.post(
        "/api/projects",
        headers=key(),
        json={"name": "Activity Source"},
    )
    assert created.status_code == 201

    activity = client.get("/api/activity")
    events = client.get("/api/events")

    assert activity.status_code == 200
    assert activity.json() == {"items": [], "limit": 100, "offset": 0}
    assert events.status_code == 200
    assert events.json()["items"][0]["event_type"] == "project.created"
    assert events.json()["last_event_id"] >= 1


def test_settings_read_save_and_storage_report_are_real(client: TestClient) -> None:
    current = client.get("/api/settings")
    storage = client.get("/api/settings/storage")
    saved = client.put(
        "/api/settings",
        headers=key(),
        json={"overlay": {"renderer": {"width": 1280, "height": 720}}},
    )

    assert current.status_code == 200
    assert '"api_key"' not in current.text
    assert storage.status_code == 200
    assert {item["name"] for item in storage.json()["categories"]} == {
        "database",
        "materials",
        "projects",
        "direct",
        "logs",
    }
    assert saved.status_code == 200
    assert saved.json()["restart_required"] is True
    assert saved.json()["settings"]["values"]["renderer"]["width"] == 1280
