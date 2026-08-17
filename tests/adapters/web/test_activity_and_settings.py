from __future__ import annotations

import json
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
from cutmaster.application.settings.provider_connections import ProviderProbe
from cutmaster.workflow.contracts.render_plan import RenderPlan


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
    assert activity.json() == {
        "items": [],
        "limit": 100,
        "offset": 0,
        "has_more": False,
    }
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
    for material in (video, music):
        with application.materials.lease(material.material_id) as binding:
            if binding.material.material_type.value == "music":
                (binding.memory_root / "music_memory.json").write_text(
                    json.dumps(
                        {
                            "schema_version": "2.0",
                            "source_duration_sec": 60.0,
                            "tempo_bpm": 120.0,
                            "beats_sec": [],
                            "accents_sec": [],
                            "energy_step_sec": 0.5,
                            "energy_curve": [],
                            "sections": [],
                        }
                    ),
                    encoding="utf-8",
                )
            staged = tmp_path / f"{material.material_id}-analysis-result.json"
            staged.write_text(
                json.dumps(
                    {
                        "schema_version": "3.0",
                        "status": "success",
                        "material_id": str(binding.material.material_id),
                        "material_type": binding.material.material_type.value,
                        "material_name": binding.material.name,
                        "material_fingerprint": str(binding.material.fingerprint),
                        "memory_schema_version": (
                            "3.0"
                            if binding.material.material_type.value == "video"
                            else "2.0"
                        ),
                        "elapsed_sec": 0.0,
                        "material_reused": False,
                        "analysis_reused": False,
                        **(
                            {
                                "model_usage_summary": {},
                                "model_usage_cumulative_summary": {},
                            }
                            if binding.material.material_type.value == "video"
                            else {}
                        ),
                    }
                ),
                encoding="utf-8",
            )
            application.materials.publish_analysis_result(binding, staged)
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
    run = application.runs.create(CreateRunCommand(str(uuid4()), project.project_id))
    claimed = application.jobs.claim_next(
        ClaimJobCommand("navigation-worker", 42, run.job.job_id)
    )
    assert claimed is not None
    relative_plan = f"projects/{project.project_id}/runs/{run.run.run_id}/plan.json"
    with application.materials.lease(video.material_id) as binding:
        video_fingerprint = binding.material.fingerprint
    with application.materials.lease(music.material_id) as binding:
        music_fingerprint = binding.material.fingerprint
    RenderPlan.create(
        video_material_id=video.material_id,
        video_expected_fingerprint=video_fingerprint,
        music_material_id=music.material_id,
        music_expected_fingerprint=music_fingerprint,
        fps=30,
        clips=[
            {
                "timestamp": "00:00:00,000-00:00:01,000",
                "output_frame_range": [0, 30],
            }
        ],
        planners_metadata={"test": True},
    ).write(application.settings.get().data_root / relative_plan)
    completed = application.runs.complete(
        CompleteRunCommand(
            str(uuid4()),
            run.run.run_id,
            run.attempt.attempt_id,
            relative_plan,
        )
    )
    render = application.renders.create(
        CreateRenderVariantCommand(
            str(uuid4()),
            completed.frozen_edit.edit_id,
            "dialogue",
        )
    )
    material_attempt = application.jobs.enqueue_material_analysis(
        EnqueueMaterialAnalysisCommand(str(uuid4()), video.material_id)
    )

    with TestClient(
        create_app(application=application, enable_job_supervisor=False)
    ) as client:
        response = client.get("/api/activity")
        first_page = client.get("/api/activity?limit=1")
        second_page = client.get("/api/activity?limit=1&offset=1")

    assert response.status_code == 200
    assert first_page.json()["has_more"] is True
    assert first_page.json()["offset"] == 0
    assert len(first_page.json()["items"]) == 1
    assert second_page.json()["offset"] == 1
    assert len(second_page.json()["items"]) == 1
    assert (
        first_page.json()["items"][0]["attempt"]["attempt_id"]
        != second_page.json()["items"][0]["attempt"]["attempt_id"]
    )
    by_owner = {
        (item["attempt"]["owner_type"], item["attempt"]["owner_id"]): item
        for item in response.json()["items"]
    }
    assert by_owner[("run", str(run.run.run_id))]["navigation"] == {
        "type": "run",
        "project_id": str(project.project_id),
        "project_name": "Activity Navigation",
        "run_id": str(run.run.run_id),
        "run_sequence": 1,
    }
    assert by_owner[("material", str(video.material_id))]["navigation"] == {
        "type": "material",
        "material_type": "video",
        "material_id": str(video.material_id),
        "material_name": "Navigation Video",
    }
    assert by_owner[("render_variant", str(render.render_variant.render_variant_id))][
        "navigation"
    ] == {
        "type": "render_variant",
        "project_id": str(project.project_id),
        "project_name": "Activity Navigation",
        "run_id": str(run.run.run_id),
        "run_sequence": 1,
        "edit_id": str(completed.frozen_edit.edit_id),
        "edit_version": 1,
        "render_variant_id": str(render.render_variant.render_variant_id),
        "audio_mode": "dialogue",
    }
    assert material_attempt.attempt.owner_id == str(video.material_id)


def test_settings_read_save_and_storage_report_are_real(client: TestClient) -> None:
    current = client.get("/api/settings")
    storage = client.get("/api/settings/storage")
    saved = client.put(
        "/api/settings",
        headers=key(),
        json={"values": {"renderer": {"width": 1280, "height": 720}}},
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


def test_storage_reveal_calls_the_real_host_capability_idempotently(
    client: TestClient,
    application: CutMasterApplication,
) -> None:
    opened: list[Path] = []
    application.settings._storage_revealer = opened.append
    application.settings._reveal_supported = True
    headers = key()

    capability = client.get("/api/settings/storage")
    first = client.post("/api/settings/storage/reveal", headers=headers)
    replay = client.post("/api/settings/storage/reveal", headers=headers)

    assert capability.json()["reveal_supported"] is True
    assert first.status_code == 200
    assert first.json() == {"opened": True}
    assert replay.json() == {"opened": True}
    assert opened == [application.settings.get().data_root]


def test_provider_settings_save_is_secret_free_and_idempotent(
    client: TestClient,
    config_path: Path,
) -> None:
    identifier = str(uuid4())
    headers = {"Idempotency-Key": identifier}
    secret = "web-secret-value"
    payload = {
        "profile": "simple",
        "credentials": {
            capability: {"action": "set", "value": secret}
            for capability in ("llm", "vlm", "asr")
        },
    }

    saved = client.put("/api/settings/providers", headers=headers, json=payload)
    replay = client.put("/api/settings/providers", headers=headers, json=payload)

    assert saved.status_code == 200
    assert replay.status_code == 200
    assert secret not in saved.text
    assert saved.json()["settings"]["connections"]["profile"] == "simple"
    assert saved.json()["credential_results"] == {
        "llm": "set",
        "vlm": "set",
        "asr": "set",
    }
    assert (config_path.parent / ".env").stat().st_mode & 0o777 == 0o600

    drifted = {
        **payload,
        "credentials": {
            capability: {"action": "set", "value": "another-secret"}
            for capability in ("llm", "vlm", "asr")
        },
    }
    conflict = client.put(
        "/api/settings/providers",
        headers=headers,
        json=drifted,
    )
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"


def test_provider_connection_test_uses_ephemeral_secret_without_echoing_it(
    client: TestClient,
    application: CutMasterApplication,
) -> None:
    probes: list[ProviderProbe] = []

    def tester(probe: ProviderProbe) -> float:
        probes.append(probe)
        return 8.75

    application.settings._connection_tester = tester
    current = client.get("/api/settings").json()
    configuration = current["connections"]["providers"]["llm"]
    configuration["base_url"] = "https://models.example.test/v1"
    secret = "ephemeral-web-secret"

    response = client.post(
        "/api/settings/providers/llm/test",
        json={"configuration": configuration, "api_key": secret},
    )

    assert response.status_code == 200
    assert response.json() == {
        "capability": "llm",
        "status": "connected",
        "latency_ms": 8.8,
    }
    assert secret not in response.text
    assert probes[0].api_key == secret


def test_preset_provider_payload_cannot_override_server_canonical_values(
    client: TestClient,
) -> None:
    current = client.get("/api/settings").json()["connections"]["providers"]
    response = client.put(
        "/api/settings/providers",
        headers=key(),
        json={
            "profile": "simple",
            "providers": current,
            "credentials": {
                capability: {"action": "keep"} for capability in ("llm", "vlm", "asr")
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "request_validation_failed"
