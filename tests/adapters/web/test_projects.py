from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cutmaster.application import CutMasterApplication


def key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


def test_project_crud_brief_search_sort_and_workspace_are_application_backed(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/projects",
        headers=key(),
        json={"name": "  Film Project  "},
    )
    assert created.status_code == 201
    project = created.json()
    project_id = project["project_id"]
    assert project["name"] == "Film Project"

    renamed = client.post(
        f"/api/projects/{project_id}/rename",
        headers=key(),
        json={"name": "Opening Cut"},
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Opening Cut"

    brief = client.put(
        f"/api/projects/{project_id}/creative-brief",
        headers=key(),
        json={"editing_intent": "A tense reunion", "target_duration_sec": 60},
    )
    assert brief.status_code == 200
    assert brief.json()["creative_brief"] == {
        "editing_intent": "A tense reunion",
        "target_duration_sec": 60.0,
    }

    listing = client.get("/api/projects?search=opening&sort=name_desc")
    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    assert listing.json()["items"][0]["project_id"] == project_id

    workspace = client.get(f"/api/projects/{project_id}/workspace")
    assert workspace.status_code == 200
    assert workspace.json() == {
        "project": brief.json(),
        "materials": {"video": [], "music": []},
        "runs": [],
    }

    delete_key = key()
    deleted = client.delete(f"/api/projects/{project_id}", headers=delete_key)
    replayed_delete = client.delete(f"/api/projects/{project_id}", headers=delete_key)
    assert deleted.status_code == replayed_delete.status_code == 204
    not_found = client.get(f"/api/projects/{project_id}")
    assert not_found.status_code == 404
    assert not_found.json()["code"] == "resource_not_found"


def test_project_commands_are_durably_idempotent(client: TestClient) -> None:
    command = key()
    first = client.post(
        "/api/projects",
        headers=command,
        json={"name": "One"},
    )
    replay = client.post(
        "/api/projects",
        headers=command,
        json={"name": "One"},
    )
    conflict = client.post(
        "/api/projects",
        headers=command,
        json={"name": "Two"},
    )

    assert first.status_code == replay.status_code == 201
    assert first.json() == replay.json()
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"


def test_project_setup_atomically_saves_materials_and_brief(
    client: TestClient,
    application: CutMasterApplication,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created = client.post(
        "/api/projects",
        headers=key(),
        json={"name": "Unified setup"},
    ).json()
    video_path = tmp_path / "setup.mp4"
    music_path = tmp_path / "setup.mp3"
    video_path.write_bytes(b"video")
    music_path.write_bytes(b"music")
    video = application.materials.add(video_path, "video", "Setup Video")
    music = application.materials.add(music_path, "music", "Setup Music")

    response = client.put(
        f"/api/projects/{created['project_id']}/setup",
        headers=key(),
        json={
            "video_material_ids": [str(video.material_id)],
            "music_material_ids": [str(music.material_id)],
            "editing_intent": "Build an uplifting finale",
            "target_duration_sec": 45,
        },
    )

    assert response.status_code == 200
    assert response.json()["video_material_ids"] == [str(video.material_id)]
    assert response.json()["music_material_ids"] == [str(music.material_id)]
    assert response.json()["creative_brief"] == {
        "editing_intent": "Build an uplifting finale",
        "target_duration_sec": 45.0,
    }
    persisted = client.get(f"/api/projects/{created['project_id']}").json()
    assert persisted == response.json()

    def reject_heavy_detail(*_args, **_kwargs):
        raise AssertionError("Project workspace must not acquire Material read leases")

    monkeypatch.setattr(type(application.materials), "detail", reject_heavy_detail)
    workspace = client.get(f"/api/projects/{created['project_id']}/workspace")
    assert workspace.status_code == 200
    assert workspace.json()["materials"] == {
        "video": [
            {
                "material_id": str(video.material_id),
                "material_type": "video",
                "name": "Setup Video",
                "condition": "queued",
                "reused": False,
            }
        ],
        "music": [
            {
                "material_id": str(music.material_id),
                "material_type": "music",
                "name": "Setup Music",
                "condition": "queued",
                "reused": False,
            }
        ],
    }
    card = client.get("/api/projects").json()["items"][0]
    assert card["latest_run_state"] is None
    assert card["selected_materials"] == workspace.json()["materials"]
    assert card["preview_url"] is None
