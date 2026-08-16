from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from cutmaster.adapters.web import create_app
from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import (
    ClaimJobCommand,
    EnqueueMaterialAnalysisCommand,
)
from cutmaster.application.projects import (
    CreateProjectCommand,
    SaveCreativeBriefCommand,
    SetProjectMaterialsCommand,
)
from cutmaster.application.renders import CreateRenderVariantCommand
from cutmaster.application.runs import CompleteRunCommand, CreateRunCommand


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


def test_activity_projects_canonical_navigation_context_for_each_owner(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = application.projects.create(
        CreateProjectCommand(str(uuid4()), "Activity Navigation")
    )
    video_path = tmp_path / "navigation.mp4"
    music_path = tmp_path / "navigation.mp3"
    video_path.write_bytes(b"video")
    music_path.write_bytes(b"music")
    video = application.materials.add(video_path, "video", "Navigation Video")
    music = application.materials.add(music_path, "music", "Navigation Music")
    application.projects.set_materials(
        SetProjectMaterialsCommand(
            str(uuid4()),
            project.project_id,
            (video.material_id,),
            (music.material_id,),
        )
    )
    application.projects.save_creative_brief(
        SaveCreativeBriefCommand(
            str(uuid4()),
            project.project_id,
            "Build a navigable edit",
            30,
        )
    )
    run = application.runs.create(
        CreateRunCommand(str(uuid4()), project.project_id)
    )
    claimed = application.jobs.claim_next(
        ClaimJobCommand("navigation-worker", 42, run.job.job_id)
    )
    assert claimed is not None
    completed = application.runs.complete(
        CompleteRunCommand(
            str(uuid4()),
            run.run.run_id,
            run.attempt.attempt_id,
            f"projects/{project.project_id}/runs/{run.run.run_id}/plan.json",
        )
    )
    render = application.renders.create(
        CreateRenderVariantCommand(
            str(uuid4()),
            completed.frozen_edit.edit_id,
            {"audio_mode": "dialogue"},
        )
    )
    material_attempt = application.jobs.enqueue_material_analysis(
        EnqueueMaterialAnalysisCommand(str(uuid4()), video.material_id)
    )

    with TestClient(create_app(application=application)) as client:
        response = client.get("/api/activity")

    assert response.status_code == 200
    by_owner = {
        (item["attempt"]["owner_type"], item["attempt"]["owner_id"]): item
        for item in response.json()["items"]
    }
    assert by_owner[("run", str(run.run.run_id))]["navigation"] == {
        "type": "run",
        "project_id": str(project.project_id),
        "run_id": str(run.run.run_id),
    }
    assert by_owner[("material", str(video.material_id))]["navigation"] == {
        "type": "material",
        "material_type": "video",
        "material_id": str(video.material_id),
    }
    assert by_owner[
        ("render_variant", str(render.render_variant.render_variant_id))
    ]["navigation"] == {
        "type": "render_variant",
        "project_id": str(project.project_id),
        "run_id": str(run.run.run_id),
        "edit_id": str(completed.frozen_edit.edit_id),
        "render_variant_id": str(render.render_variant.render_variant_id),
    }
    assert material_attempt.attempt.owner_id == str(video.material_id)


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
