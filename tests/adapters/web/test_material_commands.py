from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from cutmaster.adapters.web import create_app
from cutmaster.adapters.web.material_worker import execute_material_job
from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import (
    ClaimJobCommand,
    FailAttemptCommand,
    JobsService,
    JobSubmissionView,
    StopAttemptCommand,
)
from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialCondition
from cutmaster.infrastructure.storage.local.material_catalog import (
    MaterialInconsistentError,
)


def command_id() -> str:
    return str(uuid4())


class CapturingMaterialDispatcher:
    def __init__(self) -> None:
        self.submissions: list[JobSubmissionView] = []

    def dispatch(self, submission: JobSubmissionView) -> None:
        self.submissions.append(submission)


def _client(
    application: CutMasterApplication,
    dispatcher: CapturingMaterialDispatcher,
) -> TestClient:
    return TestClient(
        create_app(
            application=application,
            material_dispatcher=dispatcher,
        )
    )


def _import_video(
    client: TestClient,
    *,
    key: str,
    name: str = "Web Film",
    value: bytes = b"video bytes",
    subtitle: bytes | None = b"1\n00:00:00,000 --> 00:00:01,000\nHello\n",
):
    files = {"source": ("film.mp4", value, "video/mp4")}
    if subtitle is not None:
        files["subtitle"] = ("film.srt", subtitle, "application/x-subrip")
    return client.post(
        "/api/materials",
        headers={"Idempotency-Key": key},
        data={"material_type": "video", "name": name},
        files=files,
    )


def _publish_video_memory(
    application: CutMasterApplication,
    material,
    workspace: Path,
) -> None:
    with application.materials.lease(material.material_id) as binding:
        (binding.memory_root / "video_description.json").write_text(
            json.dumps(
                {
                    "schema_version": "3.0",
                    "source": {
                        "title": material.name,
                        "duration_sec": 12.0,
                        "fps": 24.0,
                        "width": 640,
                        "height": 360,
                    },
                    "segments": [],
                }
            ),
            encoding="utf-8",
        )
        (binding.memory_root / "video_summary.json").write_text(
            json.dumps({"schema_version": "1.0", "synopsis": "Test"}),
            encoding="utf-8",
        )
        (binding.memory_root / "dialogues.json").write_text(
            json.dumps(
                {
                    "schema_version": "2.0",
                    "statistics": {"sentence_count": 0},
                    "sentences": [],
                }
            ),
            encoding="utf-8",
        )
        staged = workspace / "analysis_result.json"
        staged.write_text(
            json.dumps(
                {
                    "schema_version": "3.0",
                    "status": "success",
                    "material_id": str(binding.material.material_id),
                    "material_type": "video",
                    "material_name": binding.material.name,
                    "material_fingerprint": str(binding.material.fingerprint),
                    "memory_schema_version": "3.0",
                    "elapsed_sec": 0.0,
                    "material_reused": False,
                    "analysis_reused": False,
                    "model_usage_summary": {},
                    "model_usage_cumulative_summary": {},
                }
            ),
            encoding="utf-8",
        )
        application.materials.publish_analysis_result(binding, staged)


def _publish_music_memory(
    application: CutMasterApplication,
    material,
    workspace: Path,
) -> None:
    assert material.material_type.value == "music"
    with application.materials.lease(material.material_id) as binding:
        (binding.memory_root / "music_memory.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "source_duration_sec": 9.0,
                    "tempo_bpm": 100.0,
                    "beats_sec": [0.6],
                    "accents_sec": [0.6],
                    "energy_step_sec": 0.5,
                    "energy_curve": [],
                    "sections": [],
                }
            ),
            encoding="utf-8",
        )
        staged = workspace / "music_analysis_result.json"
        staged.write_text(
            json.dumps(
                {
                    "schema_version": "3.0",
                    "status": "success",
                    "material_id": str(binding.material.material_id),
                    "material_type": "music",
                    "material_name": binding.material.name,
                    "material_fingerprint": str(binding.material.fingerprint),
                    "memory_schema_version": "2.0",
                    "elapsed_sec": 0.0,
                    "material_reused": False,
                    "analysis_reused": False,
                }
            ),
            encoding="utf-8",
        )
        application.materials.publish_analysis_result(binding, staged)


def test_import_preflight_idempotency_and_type_scoped_collision(
    application: CutMasterApplication,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "cutmaster.application.materials.service._material_duration",
        lambda *_args: 12.0,
    )
    dispatcher = CapturingMaterialDispatcher()
    key = command_id()
    with _client(application, dispatcher) as client:
        available = client.post(
            "/api/materials/preflight",
            json={"material_type": "video", "name": "  Web   Film  "},
        )
        first = _import_video(client, key=key)
        replay = _import_video(client, key=key)
        changed_replay = _import_video(client, key=key, value=b"different")
        collision = _import_video(client, key=command_id())
        occupied = client.post(
            "/api/materials/preflight",
            json={"material_type": "video", "name": "Web Film"},
        )
        other_type = client.post(
            "/api/materials/preflight",
            json={"material_type": "music", "name": "Web Film"},
        )

    assert available.status_code == 200
    assert available.json() == {
        "available": True,
        "normalized_name": "Web Film",
        "existing_material": None,
    }
    assert first.status_code == replay.status_code == 202
    assert first.headers["location"].startswith("/api/materials/mat_")
    assert first.json()["material"]["condition"] == "queued"
    assert first.json()["material"]["thumbnail_url"] is None
    assert first.json()["material"]["waveform_url"] is None
    assert first.json()["material"]["latest_execution"]["attempt"]["status"] == "queued"
    assert (
        replay.json()["material"]["material_id"]
        == first.json()["material"]["material_id"]
    )
    assert (
        replay.json()["attempt"]["attempt_id"] == first.json()["attempt"]["attempt_id"]
    )
    assert changed_replay.status_code == 409
    assert changed_replay.json()["code"] == "idempotency_conflict"
    assert collision.status_code == 409
    assert collision.json()["code"] == "material_name_collision"
    assert occupied.json()["available"] is False
    assert (
        occupied.json()["existing_material"]["material_id"]
        == first.json()["material"]["material_id"]
    )
    assert other_type.json()["available"] is True
    # A still-queued replay is dispatched again; the production supervisor
    # de-duplicates live processes and this also repairs a lost first wake-up.
    assert len(dispatcher.submissions) == 2

    material_id = MaterialId.parse(dispatcher.submissions[0].attempt.owner_id)
    with application.materials.lease(material_id) as binding:
        assert application.materials.resolve_subtitle(binding) is not None


def test_material_worker_and_retry_project_real_lifecycle(
    application: CutMasterApplication,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "cutmaster.application.materials.service._material_duration",
        lambda *_args: 12.0,
    )
    dispatcher = CapturingMaterialDispatcher()
    with _client(application, dispatcher) as client:
        imported = _import_video(client, key=command_id(), name="Retry Film")
        submission = dispatcher.submissions[-1]
        claimed = application.jobs.claim_next(
            ClaimJobCommand("test-material-worker", 4321, submission.job.job_id)
        )
        assert claimed is not None
        running = client.get(
            f"/api/materials/{imported.json()['material']['material_id']}"
        )
        application.jobs.mark_failed(
            FailAttemptCommand(claimed.attempt.attempt_id, "provider unavailable")
        )
        failed = client.get(
            f"/api/materials/{imported.json()['material']['material_id']}"
        )
        retry_key = command_id()
        retried = client.post(
            f"/api/materials/{imported.json()['material']['material_id']}/retry",
            headers={"Idempotency-Key": retry_key},
        )

    assert running.json()["condition"] == "analysing"
    assert running.json()["latest_execution"]["attempt"]["status"] == "running"
    assert failed.json()["condition"] == "failed"
    assert (
        failed.json()["latest_execution"]["attempt"]["error_message"]
        == "provider unavailable"
    )
    assert retried.status_code == 202
    assert retried.json()["attempt"]["sequence"] == 2

    retry_submission = dispatcher.submissions[-1]
    outcome = execute_material_job(
        application,
        retry_submission.job.job_id,
        analyser=_publish_video_memory,
        heartbeat_interval_sec=0.01,
    )
    assert outcome is not None and outcome.value == "complete"
    with _client(application, CapturingMaterialDispatcher()) as client:
        ready = client.get(
            f"/api/materials/{imported.json()['material']['material_id']}"
        )
        replayed_retry = client.post(
            f"/api/materials/{imported.json()['material']['material_id']}/retry",
            headers={"Idempotency-Key": retry_key},
        )
        invalid_retry = client.post(
            f"/api/materials/{imported.json()['material']['material_id']}/retry",
            headers={"Idempotency-Key": command_id()},
        )
    assert ready.json()["condition"] == "ready"
    assert ready.json()["analysis_available"] is True
    assert replayed_retry.status_code == 202
    assert (
        replayed_retry.json()["attempt"]["attempt_id"]
        == retried.json()["attempt"]["attempt_id"]
    )
    assert invalid_retry.status_code == 409
    assert invalid_retry.json()["code"] == "material_already_ready"


def test_music_import_runs_through_same_real_worker_boundary(
    application: CutMasterApplication,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "cutmaster.application.materials.service._material_duration",
        lambda *_args: 9.0,
    )
    dispatcher = CapturingMaterialDispatcher()
    with _client(application, dispatcher) as client:
        invalid_subtitle = client.post(
            "/api/materials",
            headers={"Idempotency-Key": command_id()},
            data={"material_type": "music", "name": "Invalid Music"},
            files={
                "source": ("music.wav", b"music", "audio/wav"),
                "subtitle": ("music.srt", b"subtitle", "application/x-subrip"),
            },
        )
        imported = client.post(
            "/api/materials",
            headers={"Idempotency-Key": command_id()},
            data={"material_type": "music", "name": "Web Score"},
            files={"source": ("music.wav", b"music bytes", "audio/wav")},
        )

    assert invalid_subtitle.status_code == 422
    assert invalid_subtitle.json()["code"] == "invalid_request"
    assert imported.status_code == 202
    submission = dispatcher.submissions[-1]
    outcome = execute_material_job(
        application,
        submission.job.job_id,
        analyser=_publish_music_memory,
        heartbeat_interval_sec=0.01,
    )
    assert outcome is not None and outcome.value == "complete"
    with _client(application, CapturingMaterialDispatcher()) as client:
        ready = client.get(
            f"/api/materials/{imported.json()['material']['material_id']}"
        )
    assert ready.json()["condition"] == "ready"
    assert ready.json()["memory_summary"]["tempo_bpm"] == 100.0


def test_material_stop_before_canonical_publish_is_interrupted_and_not_ready(
    application: CutMasterApplication,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "cutmaster.application.materials.service._material_duration",
        lambda *_args: 12.0,
    )
    dispatcher = CapturingMaterialDispatcher()
    with _client(application, dispatcher) as client:
        _import_video(client, key=command_id(), name="Stopped Before Publish")
    submission = dispatcher.submissions[-1]

    def stop_before_publish(app, _material, _workspace):
        app.jobs.stop(StopAttemptCommand(command_id(), submission.attempt.attempt_id))

    outcome = execute_material_job(
        application,
        submission.job.job_id,
        analyser=stop_before_publish,
        heartbeat_interval_sec=0.01,
    )

    material_id = MaterialId.parse(submission.attempt.owner_id)
    material = application.materials.get(material_id)
    attempt = application.jobs.get_attempt(submission.attempt.attempt_id)
    assert outcome is not None and outcome.value == "interrupted"
    assert attempt.status.value == "interrupted"
    assert material is not None
    assert material.condition is not MaterialCondition.READY
    with application.materials.lease(material_id) as binding:
        assert not (binding.memory_root / "analysis_result.json").exists()


def test_material_late_stop_after_canonical_publish_completes(
    application: CutMasterApplication,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "cutmaster.application.materials.service._material_duration",
        lambda *_args: 12.0,
    )
    dispatcher = CapturingMaterialDispatcher()
    with _client(application, dispatcher) as client:
        _import_video(client, key=command_id(), name="Stopped After Publish")
    submission = dispatcher.submissions[-1]

    def publish_then_stop(app, material, workspace):
        _publish_video_memory(app, material, workspace)
        app.jobs.stop(StopAttemptCommand(command_id(), submission.attempt.attempt_id))

    outcome = execute_material_job(
        application,
        submission.job.job_id,
        analyser=publish_then_stop,
        heartbeat_interval_sec=0.01,
    )

    material = application.materials.get(MaterialId.parse(submission.attempt.owner_id))
    attempt = application.jobs.get_attempt(submission.attempt.attempt_id)
    assert outcome is not None and outcome.value == "complete"
    assert attempt.status.value == "complete"
    assert material is not None
    assert material.condition is MaterialCondition.READY


def test_worker_projects_verified_source_mismatch_as_inconsistent(
    application: CutMasterApplication,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "cutmaster.application.materials.service._material_duration",
        lambda *_args: 12.0,
    )
    dispatcher = CapturingMaterialDispatcher()
    with _client(application, dispatcher) as client:
        imported = _import_video(client, key=command_id(), name="Inconsistent Film")

    def inconsistent(_application, _material, _workspace):
        raise MaterialInconsistentError("managed source fingerprint changed")

    outcome = execute_material_job(
        application,
        dispatcher.submissions[-1].job.job_id,
        analyser=inconsistent,
        heartbeat_interval_sec=0.01,
    )
    assert outcome is not None and outcome.value == "failed"
    with _client(application, CapturingMaterialDispatcher()) as client:
        detail = client.get(
            f"/api/materials/{imported.json()['material']['material_id']}"
        )
    assert detail.json()["condition"] == "inconsistent"
    assert detail.json()["latest_execution"]["attempt"]["status"] == "failed"


def test_worker_redacts_provider_secrets_before_persisting_failure(
    application: CutMasterApplication,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "cutmaster.application.materials.service._material_duration",
        lambda *_args: 12.0,
    )
    dispatcher = CapturingMaterialDispatcher()
    with _client(application, dispatcher) as client:
        imported = _import_video(client, key=command_id(), name="Secret Failure")
    submission = dispatcher.submissions[-1]

    def provider_failure(_application, _material, _workspace):
        raise RuntimeError("authorization: bearer-sensitive-token")

    outcome = execute_material_job(
        application,
        submission.job.job_id,
        analyser=provider_failure,
        heartbeat_interval_sec=0.01,
    )

    attempt = application.jobs.get_attempt(submission.attempt.attempt_id)
    assert outcome is not None and outcome.value == "failed"
    assert attempt.error_message is not None
    assert "bearer-sensitive-token" not in attempt.error_message
    assert "[REDACTED]" in attempt.error_message
    with _client(application, CapturingMaterialDispatcher()) as client:
        response = client.get(
            f"/api/materials/{imported.json()['material']['material_id']}"
        )
    assert "bearer-sensitive-token" not in response.text


def test_interrupted_material_resumes_and_delete_is_guarded_and_idempotent(
    application: CutMasterApplication,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "cutmaster.application.materials.service._material_duration",
        lambda *_args: 12.0,
    )
    dispatcher = CapturingMaterialDispatcher()
    with _client(application, dispatcher) as client:
        imported = _import_video(client, key=command_id(), name="Delete Film")
        material_id = imported.json()["material"]["material_id"]
        attempt_id = imported.json()["attempt"]["attempt_id"]
        blocked_active = client.delete(
            f"/api/materials/{material_id}",
            headers={"Idempotency-Key": command_id()},
        )
        stopped = client.post(
            f"/api/attempts/{attempt_id}/stop",
            headers={"Idempotency-Key": command_id()},
        )
        interrupted = client.get(f"/api/materials/{material_id}")
        resumed = client.post(
            f"/api/materials/{material_id}/resume",
            headers={"Idempotency-Key": command_id()},
        )
        resumed_attempt = resumed.json()["attempt"]["attempt_id"]
        client.post(
            f"/api/attempts/{resumed_attempt}/stop",
            headers={"Idempotency-Key": command_id()},
        )

        delete_key = command_id()
        deleted = client.delete(
            f"/api/materials/{material_id}",
            headers={"Idempotency-Key": delete_key},
        )
        replayed = client.delete(
            f"/api/materials/{material_id}",
            headers={"Idempotency-Key": delete_key},
        )
        missing = client.delete(
            f"/api/materials/{material_id}",
            headers={"Idempotency-Key": command_id()},
        )

    assert blocked_active.status_code == 409
    assert blocked_active.json()["code"] == "active_attempts"
    assert blocked_active.json()["blockers"] == [
        {"type": "attempt", "attempt_id": attempt_id}
    ]
    assert stopped.json()["attempt"]["status"] == "interrupted"
    assert interrupted.json()["condition"] == "failed"
    assert interrupted.json()["latest_execution"]["attempt"]["status"] == "interrupted"
    assert resumed.status_code == 202
    assert resumed.json()["attempt"]["sequence"] == 2
    assert deleted.status_code == replayed.status_code == 204
    assert missing.status_code == 404
    assert (
        application.jobs.activity(
            owner_type="material",
            owner_id=material_id,
        )
        == ()
    )


def test_delete_replay_finishes_cleanup_after_catalog_commit(
    application: CutMasterApplication,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "cutmaster.application.materials.service._material_duration",
        lambda *_args: 12.0,
    )
    dispatcher = CapturingMaterialDispatcher()
    delete_key = command_id()
    original_purge = JobsService.purge_material_history
    calls = 0

    def fail_once(self, material_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated post-Catalog crash")
        return original_purge(self, material_id)

    with TestClient(
        create_app(
            application=application,
            material_dispatcher=dispatcher,
        ),
        raise_server_exceptions=False,
    ) as client:
        imported = _import_video(client, key=command_id(), name="Crash Safe Delete")
        material_id = imported.json()["material"]["material_id"]
        client.post(
            f"/api/attempts/{imported.json()['attempt']['attempt_id']}/stop",
            headers={"Idempotency-Key": command_id()},
        )
        monkeypatch.setattr(JobsService, "purge_material_history", fail_once)
        failed = client.delete(
            f"/api/materials/{material_id}",
            headers={"Idempotency-Key": delete_key},
        )
        replayed = client.delete(
            f"/api/materials/{material_id}",
            headers={"Idempotency-Key": delete_key},
        )

    assert failed.status_code == 500
    assert application.materials.get(MaterialId.parse(material_id)) is None
    assert replayed.status_code == 204
    assert (
        application.jobs.activity(
            owner_type="material",
            owner_id=material_id,
        )
        == ()
    )
