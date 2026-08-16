from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import PropertyMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from cutmaster.adapters.web import create_app
from cutmaster.adapters.web.run_worker import (
    ASTERJobProgressReporter,
    execute_run_job,
)
from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import (
    ClaimJobCommand,
    FailAttemptCommand,
    HeartbeatJobCommand,
    StopAttemptCommand,
)
from cutmaster.application.projects import (
    CreateProjectCommand,
    SaveCreativeBriefCommand,
    SetProjectMaterialsCommand,
)
from cutmaster.application.renders import CreateRenderVariantCommand
from cutmaster.application.runs import CreateRunCommand, RunSubmissionView
from cutmaster.domain.runs import RunStatus
from cutmaster.workflow.contracts.render_plan import RenderPlan
from cutmaster.workflow.contracts.checkpoints import (
    PlannersCheckpoint,
    PlannersCheckpointStage,
)
from cutmaster.workflow.ports import ProgressUpdate


def command_id() -> str:
    return str(uuid4())


class CapturingDispatcher:
    def __init__(self) -> None:
        self.submissions: list[RunSubmissionView] = []

    def dispatch(self, submission: RunSubmissionView) -> None:
        self.submissions.append(submission)


def prepared_project(
    application: CutMasterApplication,
    tmp_path: Path,
    *,
    music_duration_sec: float = 90.0,
    target_duration_sec: float = 45.0,
    ready: bool = True,
):
    project = application.projects.create(CreateProjectCommand(command_id(), "Web Run"))
    video_path = tmp_path / f"{uuid4()}.mp4"
    music_path = tmp_path / f"{uuid4()}.mp3"
    video_path.write_bytes(b"video")
    music_path.write_bytes(b"music")
    video = application.materials.add(video_path, "video", f"Video {uuid4()}")
    music = application.materials.add(music_path, "music", f"Music {uuid4()}")
    for material in (video, music) if ready else ():
        with application.materials.lease(material.material_id) as binding:
            if material.material_type.value == "music":
                (binding.memory_root / "music_memory.json").write_text(
                    json.dumps({"source_duration_sec": music_duration_sec}),
                    encoding="utf-8",
                )
            staged = tmp_path / f"{material.material_id}-analysis-result.json"
            staged.write_text(
                json.dumps(
                    {
                        "schema_version": "2.0",
                        "status": "success",
                        "material_id": str(binding.material.material_id),
                        "material_type": binding.material.material_type.value,
                        "material_name": binding.material.name,
                        "material_fingerprint": str(binding.material.fingerprint),
                    }
                ),
                encoding="utf-8",
            )
            application.materials.publish_analysis_result(binding, staged)
    application.projects.set_materials(
        SetProjectMaterialsCommand(
            command_id(),
            project.project_id,
            (video.material_id,),
            (music.material_id,),
        )
    )
    return application.projects.save_creative_brief(
        SaveCreativeBriefCommand(
            command_id(),
            project.project_id,
            "Build a tense reunion",
            target_duration_sec,
        )
    )


def fake_planner(_application, run, workspace: Path) -> Path:
    assert run.creative_brief.editing_intent == "Build a tense reunion"
    assert run.creative_brief.target_duration_sec == 45
    path = workspace / "render_plan.json"
    path.write_bytes(b'{"render_plan":"real-worker-artifact"}\n')
    return path


def usage_summary(
    *,
    model: str = "qwen-max",
    task: str = "story_editor",
    total_tokens: int = 120,
    total_cost_yuan: float = 0.012,
) -> dict[str, object]:
    prompt_tokens = total_tokens - 20
    cached_prompt_tokens = min(10, prompt_tokens)
    summary: dict[str, object] = {
        "currency": "CNY",
        "price_unit": "yuan_per_million_tokens",
        "request_count": 1,
        "reported_usage_count": 1,
        "unreported_usage_count": 0,
        "priced_usage_count": 1,
        "unpriced_usage_count": 0,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": 20,
        "total_tokens": total_tokens,
        "cached_prompt_tokens": cached_prompt_tokens,
        "uncached_prompt_tokens": prompt_tokens - cached_prompt_tokens,
        "reasoning_tokens": 5,
        "uncached_input_cost_yuan": total_cost_yuan * 0.4,
        "cached_input_cost_yuan": total_cost_yuan * 0.1,
        "output_cost_yuan": total_cost_yuan * 0.5,
        "total_cost_yuan": total_cost_yuan,
        "by_model": {},
        "by_task": {},
    }
    bucket = {
        key: value
        for key, value in summary.items()
        if key not in {"currency", "price_unit", "by_model", "by_task"}
    }
    summary["by_model"] = {model: dict(bucket)}
    summary["by_task"] = {task: dict(bucket)}
    return summary


def write_usage_artifact(
    workspace: Path,
    summary: dict[str, object],
) -> None:
    diagnostics = workspace / "diagnostics"
    diagnostics.mkdir(parents=True, exist_ok=True)
    private_summary = {
        **summary,
        "calls": [{"response_id": "private-provider-response"}],
        "provider_usage": {"raw": True},
        "artifact": "/private/planners/model_usage.json",
    }
    (diagnostics / "model_usage.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "current_run": {
                    "summary": private_summary,
                    "call_ids": ["private-call-id"],
                },
                "cumulative": {
                    "summary": usage_summary(
                        total_tokens=999_999,
                        total_cost_yuan=999.0,
                    )
                },
                "calls": [{"request": {"api_key": "secret"}}],
            }
        ),
        encoding="utf-8",
    )


def test_start_run_dispatches_real_job_and_worker_creates_frozen_edit(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    dispatcher = CapturingDispatcher()
    with TestClient(
        create_app(application=application, run_dispatcher=dispatcher)
    ) as client:
        response = client.post(
            f"/api/projects/{project.project_id}/runs",
            headers={"Idempotency-Key": command_id()},
        )

        assert response.status_code == 202
        payload = response.json()
        assert payload["run"]["status"] == "queued"
        assert payload["run"]["creative_brief"] == {
            "editing_intent": "Build a tense reunion",
            "target_duration_sec": 45.0,
        }
        assert payload["attempt"]["operation_type"] == "aster_planning"
        assert payload["job"]["status"] == "queued"
        assert len(dispatcher.submissions) == 1

        submission = dispatcher.submissions[0]
        status = execute_run_job(
            application,
            submission.job.job_id,
            planner=fake_planner,
            heartbeat_interval_sec=0.01,
        )
        detail = client.get(f"/api/runs/{submission.run.run_id}")

    assert status is RunStatus.COMPLETE
    assert detail.status_code == 200
    assert detail.json()["run"]["status"] == "complete"
    assert len(detail.json()["frozen_edits"]) == 1
    relative_plan = (
        f"projects/{project.project_id}/runs/{submission.run.run_id}/plan.json"
    )
    plan = application.settings.get().data_root / relative_plan
    assert plan.read_bytes() == b'{"render_plan":"real-worker-artifact"}\n'


def test_run_worker_persists_current_attempt_usage_and_projects_privately(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    submission = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )
    expected = usage_summary()

    def planner_with_usage(app, run, workspace: Path) -> Path:
        write_usage_artifact(workspace, expected)
        return fake_planner(app, run, workspace)

    status = execute_run_job(
        application,
        submission.job.job_id,
        planner=planner_with_usage,
        heartbeat_interval_sec=0.01,
    )

    with TestClient(
        create_app(application=application, run_dispatcher=CapturingDispatcher())
    ) as client:
        detail = client.get(f"/api/runs/{submission.run.run_id}")
        run_list = client.get(f"/api/projects/{project.project_id}/runs")

    assert status is RunStatus.COMPLETE
    assert detail.status_code == run_list.status_code == 200
    model_usage = detail.json()["model_usage"]
    assert model_usage["schema_version"] == "1.0"
    assert model_usage["attempt_usage"] == [
        {
            "attempt_id": str(submission.attempt.attempt_id),
            "sequence": 1,
            "status": "complete",
            "model_usage_summary": model_usage["run_total"],
        }
    ]
    assert model_usage["run_total"]["total_tokens"] == 120
    assert model_usage["run_total"]["total_cost_yuan"] == 0.012
    assert set(model_usage["run_total"]["by_model"]) == {"qwen-max"}
    compact = run_list.json()["items"][0]["model_usage_total"]
    assert compact["total_tokens"] == 120
    assert compact["total_cost_yuan"] == 0.012
    assert "by_model" not in compact
    assert "by_task" not in compact
    serialized = json.dumps({"detail": detail.json(), "list": run_list.json()})
    for private_value in (
        "calls",
        "provider_usage",
        "response_id",
        "api_key",
        "private-call-id",
        "/private/",
        "999999",
    ):
        assert private_value not in serialized


def test_run_worker_preserves_failure_and_records_calls_already_made(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    submission = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )

    def failing_planner(_application, _run, workspace: Path):
        write_usage_artifact(
            workspace,
            usage_summary(total_tokens=75, total_cost_yuan=0.004),
        )
        raise RuntimeError("provider rejected after one billed call")

    status = execute_run_job(
        application,
        submission.job.job_id,
        planner=failing_planner,
        heartbeat_interval_sec=0.01,
    )

    attempt = application.jobs.get_attempt(submission.attempt.attempt_id)
    run = application.runs.get(submission.run.run_id)
    assert status is RunStatus.FAILED
    assert run.failure_message == "provider rejected after one billed call"
    assert attempt.model_usage_summary is not None
    assert attempt.model_usage_summary["total_tokens"] == 75
    assert attempt.model_usage_summary["total_cost_yuan"] == 0.004


def test_run_worker_records_usage_before_cooperative_interruption(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    submission = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )

    def stopped_planner(app, run, workspace: Path) -> Path:
        write_usage_artifact(
            workspace,
            usage_summary(total_tokens=44, total_cost_yuan=0.002),
        )
        app.jobs.stop(StopAttemptCommand(command_id(), submission.attempt.attempt_id))
        return fake_planner(app, run, workspace)

    status = execute_run_job(
        application,
        submission.job.job_id,
        planner=stopped_planner,
        heartbeat_interval_sec=0.01,
    )

    attempt = application.jobs.get_attempt(submission.attempt.attempt_id)
    assert status is RunStatus.INTERRUPTED
    assert attempt.status.value == "interrupted"
    assert attempt.model_usage_summary is not None
    assert attempt.model_usage_summary["total_tokens"] == 44
    assert attempt.model_usage_summary["total_cost_yuan"] == 0.002


def test_run_worker_leaves_usage_unreported_for_invalid_or_legacy_artifacts(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)

    def invalid_planner(app, run, workspace: Path) -> Path:
        write_usage_artifact(workspace, usage_summary())
        usage_path = workspace / "diagnostics" / "model_usage.json"
        payload = json.loads(usage_path.read_text(encoding="utf-8"))
        del payload["current_run"]["summary"]["total_tokens"]
        usage_path.write_text(json.dumps(payload), encoding="utf-8")
        return fake_planner(app, run, workspace)

    def legacy_planner(app, run, workspace: Path) -> Path:
        diagnostics = workspace / "diagnostics"
        diagnostics.mkdir(parents=True, exist_ok=True)
        (diagnostics / "model_usage.json").write_text(
            json.dumps(
                {
                    "schema_version": "1.0",
                    "summary": usage_summary(),
                }
            ),
            encoding="utf-8",
        )
        return fake_planner(app, run, workspace)

    for planner in (invalid_planner, legacy_planner):
        submission = application.runs.create(
            CreateRunCommand(command_id(), project.project_id)
        )
        status = execute_run_job(
            application,
            submission.job.job_id,
            planner=planner,
            heartbeat_interval_sec=0.01,
        )
        attempt = application.jobs.get_attempt(submission.attempt.attempt_id)
        assert status is RunStatus.COMPLETE
        assert attempt.model_usage_summary is None


def test_running_run_projections_include_current_execution_and_progress(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    submission = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )
    claimed = application.jobs.claim_next(
        ClaimJobCommand(
            "projection-worker",
            4321,
            submission.job.job_id,
        )
    )
    assert claimed is not None
    progress = {
        "phase": "planners",
        "stage": "candidate_retrieval",
        "agent": "timeline_scout",
        "completed": 3,
        "total": 10,
        "unit": "slot",
    }
    application.jobs.heartbeat(HeartbeatJobCommand(submission.job.job_id, progress))

    with TestClient(
        create_app(application=application, run_dispatcher=CapturingDispatcher())
    ) as client:
        run_list = client.get(f"/api/projects/{project.project_id}/runs")
        detail = client.get(f"/api/runs/{submission.run.run_id}")
        activity = client.get("/api/activity")

    assert run_list.status_code == 200
    listed = run_list.json()["items"][0]
    assert listed["run"]["status"] == "planners"
    assert listed["execution"]["attempt"]["status"] == "running"
    assert listed["execution"]["job"]["status"] == "running"
    assert listed["execution"]["job"]["progress"] == progress
    assert listed["execution"]["job"]["heartbeat_at"] is not None

    assert detail.status_code == 200
    projected = detail.json()
    assert projected["run"]["status"] == "planners"
    assert projected["execution"] == listed["execution"]

    assert activity.status_code == 200
    activity_item = activity.json()["items"][0]
    assert {
        "attempt": activity_item["attempt"],
        "job": activity_item["job"],
    } == listed["execution"]
    assert activity_item["navigation"] == {
        "type": "run",
        "project_id": str(project.project_id),
        "project_name": "Web Run",
        "run_id": str(submission.run.run_id),
        "run_sequence": 1,
    }


def test_project_card_projects_selected_material_names_and_latest_attempt_state(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    submission = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )
    claimed = application.jobs.claim_next(
        ClaimJobCommand("card-worker", 4331, submission.job.job_id)
    )
    assert claimed is not None

    with TestClient(
        create_app(application=application, run_dispatcher=CapturingDispatcher())
    ) as client:
        response = client.get("/api/projects")

    assert response.status_code == 200
    card = response.json()["items"][0]
    assert card["project_id"] == str(project.project_id)
    assert card["latest_run_state"] == "running"
    assert [item["name"] for item in card["selected_materials"]["video"]] == [
        application.materials.get(project.video_material_ids[0]).name
    ]
    assert [item["name"] for item in card["selected_materials"]["music"]] == [
        application.materials.get(project.music_material_ids[0]).name
    ]


def test_project_run_list_does_not_access_materials(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    submission = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )

    with (
        patch.object(
            CutMasterApplication,
            "materials",
            new_callable=PropertyMock,
            side_effect=AssertionError("The lightweight Run list accessed Materials"),
        ),
        TestClient(
            create_app(
                application=application,
                run_dispatcher=CapturingDispatcher(),
            )
        ) as client,
    ):
        response = client.get(f"/api/projects/{project.project_id}/runs")

    assert response.status_code == 200
    assert response.json()["items"][0]["run"]["run_id"] == str(submission.run.run_id)


def test_run_worker_failure_is_durable_and_does_not_publish_frozen_edit(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    submission = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )

    def failing_planner(_application, _run, _workspace):
        raise RuntimeError("provider rejected the request")

    status = execute_run_job(
        application,
        submission.job.job_id,
        planner=failing_planner,
        heartbeat_interval_sec=0.01,
    )

    run = application.runs.get(submission.run.run_id)
    attempt = application.jobs.get_attempt(submission.attempt.attempt_id)
    assert status is RunStatus.FAILED
    assert run.status is RunStatus.FAILED
    assert run.failure_message == "provider rejected the request"
    assert attempt.status.value == "failed"
    assert application.runs.list_frozen_edits(run.run_id) == ()


def test_run_stop_during_agent_interrupts_before_frozen_edit_publish(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    submission = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )

    def stopped_planner(app, run, workspace: Path):
        app.jobs.stop(StopAttemptCommand(command_id(), submission.attempt.attempt_id))
        return fake_planner(app, run, workspace)

    status = execute_run_job(
        application,
        submission.job.job_id,
        planner=stopped_planner,
        heartbeat_interval_sec=0.01,
    )

    run = application.runs.get(submission.run.run_id)
    attempt = application.jobs.get_attempt(submission.attempt.attempt_id)
    plan = application.settings.get().data_root / (
        f"projects/{project.project_id}/runs/{submission.run.run_id}/plan.json"
    )
    assert status is RunStatus.INTERRUPTED
    assert run.status is RunStatus.INTERRUPTED
    assert attempt.status.value == "interrupted"
    assert application.runs.list_frozen_edits(run.run_id) == ()
    assert not plan.exists()


def test_run_worker_claims_the_dispatched_job_instead_of_an_older_job(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    older = application.runs.create(CreateRunCommand(command_id(), project.project_id))
    dispatched = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )

    execute_run_job(
        application,
        dispatched.job.job_id,
        planner=fake_planner,
        heartbeat_interval_sec=0.01,
    )

    assert application.runs.get(older.run.run_id).status is RunStatus.QUEUED
    assert application.runs.get(dispatched.run.run_id).status is RunStatus.COMPLETE


def test_aster_progress_milestones_survive_plain_heartbeats(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    submission = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )
    claimed = application.jobs.claim_next(
        ClaimJobCommand("test-progress-worker", 4242, submission.job.job_id)
    )
    assert claimed is not None
    reporter = ASTERJobProgressReporter(application, submission.job.job_id)

    reporter.report(ProgressUpdate(0, 5, "arrangement_architect", "agent"))
    reporter.report(ProgressUpdate(2, 5, "timeline_scout", "agent"))
    before = application.jobs.get_job(submission.job.job_id)

    application.jobs.heartbeat(HeartbeatJobCommand(submission.job.job_id))
    after = application.jobs.get_job(submission.job.job_id)

    assert before.progress == after.progress
    assert after.progress["agent"] == "timeline_scout"
    assert after.progress["completed"] == 2
    assert [item["state"] for item in after.progress["milestones"]] == [
        "complete",
        "complete",
        "running",
        "queued",
        "queued",
    ]


def test_start_run_rejects_unready_material_and_target_longer_than_music(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    unready = prepared_project(application, tmp_path, ready=False)
    too_long = prepared_project(
        application,
        tmp_path,
        music_duration_sec=30.0,
        target_duration_sec=45.0,
    )
    dispatcher = CapturingDispatcher()

    with TestClient(
        create_app(application=application, run_dispatcher=dispatcher)
    ) as client:
        not_ready = client.post(
            f"/api/projects/{unready.project_id}/runs",
            headers={"Idempotency-Key": command_id()},
        )
        exceeds_music = client.post(
            f"/api/projects/{too_long.project_id}/runs",
            headers={"Idempotency-Key": command_id()},
        )

    assert not_ready.status_code == 409
    assert not_ready.headers["content-type"].startswith("application/problem+json")
    assert not_ready.json()["code"] == "run_material_not_ready"
    assert exceeds_music.status_code == 409
    assert exceeds_music.json()["code"] == "target_duration_exceeds_music_duration"
    assert dispatcher.submissions == []


def test_retry_replays_and_resume_without_checkpoint_is_rejected(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    failed_run = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )
    claimed = application.jobs.claim_next(
        ClaimJobCommand("failed-worker", 9001, failed_run.job.job_id)
    )
    assert claimed is not None
    application.jobs.mark_failed(
        FailAttemptCommand(failed_run.attempt.attempt_id, "provider failed")
    )
    interrupted_run = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )
    application.jobs.stop(
        StopAttemptCommand(command_id(), interrupted_run.attempt.attempt_id)
    )
    dispatcher = CapturingDispatcher()
    retry_command = command_id()

    with TestClient(
        create_app(application=application, run_dispatcher=dispatcher)
    ) as client:
        retried = client.post(
            f"/api/runs/{failed_run.run.run_id}/retry",
            headers={"Idempotency-Key": retry_command},
        )
        replayed = client.post(
            f"/api/runs/{failed_run.run.run_id}/retry",
            headers={"Idempotency-Key": retry_command},
        )
        resumed = client.post(
            f"/api/runs/{interrupted_run.run.run_id}/resume",
            headers={"Idempotency-Key": command_id()},
        )
        invalid = client.post(
            f"/api/runs/{interrupted_run.run.run_id}/retry",
            headers={"Idempotency-Key": command_id()},
        )

    assert retried.status_code == replayed.status_code == 202
    assert retried.json() == replayed.json()
    assert retried.json()["attempt"]["attempt_id"] != str(failed_run.attempt.attempt_id)
    assert resumed.status_code == 409
    assert resumed.json()["code"] == "run_checkpoint_unavailable"
    assert invalid.status_code == 409
    assert invalid.json()["code"] == "run_recovery_not_allowed"
    assert [str(item.job.job_id) for item in dispatcher.submissions] == [
        retried.json()["job"]["job_id"],
        retried.json()["job"]["job_id"],
    ]
    assert (
        len(
            application.jobs.activity(
                owner_type="run",
                owner_id=str(failed_run.run.run_id),
            )
        )
        == 2
    )


def test_resume_validates_and_pins_completed_aster_checkpoint(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    interrupted = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )
    claimed = application.jobs.claim_next(
        ClaimJobCommand("checkpoint-worker", 9002, interrupted.job.job_id)
    )
    assert claimed is not None
    session = application.runs.checkpoint_session(
        interrupted.run.run_id,
        interrupted.attempt.attempt_id,
    )
    session.save(
        PlannersCheckpoint(
            completed_stage=PlannersCheckpointStage.ARRANGEMENT,
            aster_attempt=1,
            music_profile={"planned_duration_sec": 45.0},
            slots=({"slot_id": "slot_01", "content_description": "setup"},),
            dialogue_anchors=None,
            candidate_pool=None,
            beam_path=None,
            selection=None,
            pairwise_scores=None,
            raw_script=None,
            planners_feedback=None,
            stage_timings_sec={"slot_arrangement": 0.1},
            prior_model_usage=usage_summary(),
            prior_model_call_count=1,
        )
    )
    application.jobs.stop(
        StopAttemptCommand(command_id(), interrupted.attempt.attempt_id)
    )
    application.jobs.mark_interrupted(interrupted.attempt.attempt_id)
    dispatcher = CapturingDispatcher()

    with TestClient(
        create_app(application=application, run_dispatcher=dispatcher)
    ) as client:
        resumed = client.post(
            f"/api/runs/{interrupted.run.run_id}/resume",
            headers={"Idempotency-Key": command_id()},
        )

    assert resumed.status_code == 202
    assert resumed.json()["attempt"]["attempt_id"] != str(
        interrupted.attempt.attempt_id
    )
    assert [str(item.job.job_id) for item in dispatcher.submissions] == [
        resumed.json()["job"]["job_id"]
    ]
    pinned = application.runs._store.get_attempt_resume_checkpoint(
        dispatcher.submissions[0].attempt.attempt_id
    )
    assert pinned is not None
    assert pinned["completed_stage"] == "arrangement_architect"


def test_run_again_copies_historical_snapshot_not_current_project_setup(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    source = application.runs.create(CreateRunCommand(command_id(), project.project_id))
    execute_run_job(
        application,
        source.job.job_id,
        planner=fake_planner,
        heartbeat_interval_sec=0.01,
    )
    historical = application.runs.get(source.run.run_id)
    application.projects.set_materials(
        SetProjectMaterialsCommand(command_id(), project.project_id, (), ())
    )
    application.projects.save_creative_brief(
        SaveCreativeBriefCommand(
            command_id(),
            project.project_id,
            "A different current setup",
            5.0,
        )
    )
    dispatcher = CapturingDispatcher()
    request_id = command_id()

    with TestClient(
        create_app(application=application, run_dispatcher=dispatcher)
    ) as client:
        repeated = client.post(
            f"/api/runs/{source.run.run_id}/run-again",
            headers={"Idempotency-Key": request_id},
        )
        replayed = client.post(
            f"/api/runs/{source.run.run_id}/run-again",
            headers={"Idempotency-Key": request_id},
        )

    assert repeated.status_code == replayed.status_code == 202
    assert repeated.json() == replayed.json()
    copied = application.runs.get(
        type(source.run.run_id).parse(repeated.json()["run"]["run_id"])
    )
    assert copied.run_id != historical.run_id
    assert copied.creative_brief == historical.creative_brief
    assert copied.video_material_ids == historical.video_material_ids
    assert copied.music_material_ids == historical.music_material_ids
    assert copied.configuration == historical.configuration
    assert len(application.runs.list(project.project_id)) == 2


def test_delete_run_removes_only_owned_artifacts_and_job_logs(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    submission = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )
    execute_run_job(
        application,
        submission.job.job_id,
        planner=fake_planner,
        heartbeat_interval_sec=0.01,
    )
    edit = application.runs.list_frozen_edits(submission.run.run_id)[0]
    with application.materials.lease(submission.run.video_material_ids[0]) as binding:
        video_fingerprint = binding.material.fingerprint
    with application.materials.lease(submission.run.music_material_ids[0]) as binding:
        music_fingerprint = binding.material.fingerprint
    RenderPlan.create(
        video_material_id=submission.run.video_material_ids[0],
        video_expected_fingerprint=video_fingerprint,
        music_material_id=submission.run.music_material_ids[0],
        music_expected_fingerprint=music_fingerprint,
        fps=30,
        clips=[
            {
                "timestamp": "00:00:00,000-00:00:01,000",
                "output_frame_range": [0, 30],
            }
        ],
        planners_metadata={"test": True},
    ).write(application.settings.get().data_root / edit.plan.relative_path)
    render = application.renders.create(
        CreateRenderVariantCommand(
            command_id(),
            edit.edit_id,
            "dialogue",
        )
    )
    assert render.attempt is not None and render.job is not None
    application.jobs.stop(StopAttemptCommand(command_id(), render.attempt.attempt_id))
    root = application.settings.get().data_root
    run_directory = (
        root
        / "projects"
        / str(project.project_id)
        / "runs"
        / str(submission.run.run_id)
    )
    render_directory = (
        root
        / "projects"
        / str(project.project_id)
        / "renders"
        / str(render.render_variant.render_variant_id)
    )
    render_directory.mkdir(parents=True)
    (render_directory / "partial.mp4").write_bytes(b"partial")
    logs = root / "logs" / "jobs"
    logs.mkdir(parents=True, exist_ok=True)
    run_log = logs / f"{submission.job.job_id}.log"
    render_log = logs / f"{render.job.job_id}.log"
    run_log.write_text("run", encoding="utf-8")
    render_log.write_text("render", encoding="utf-8")
    sibling = root / "projects" / str(project.project_id) / "keep.txt"
    sibling.write_text("keep", encoding="utf-8")
    request_id = command_id()

    with TestClient(
        create_app(application=application, run_dispatcher=CapturingDispatcher())
    ) as client:
        deleted = client.delete(
            f"/api/runs/{submission.run.run_id}",
            headers={"Idempotency-Key": request_id},
        )
        replayed = client.delete(
            f"/api/runs/{submission.run.run_id}",
            headers={"Idempotency-Key": request_id},
        )
        missing = client.get(f"/api/runs/{submission.run.run_id}")

    assert deleted.status_code == replayed.status_code == 200
    assert (
        deleted.json()
        == replayed.json()
        == {
            "run_id": str(submission.run.run_id),
            "deleted": True,
        }
    )
    assert missing.status_code == 404
    assert not run_directory.exists()
    assert not render_directory.exists()
    assert not run_log.exists()
    assert not render_log.exists()
    assert sibling.read_text(encoding="utf-8") == "keep"


def test_delete_run_unlinks_exact_owner_symlink_without_following_it(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    submission = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )
    application.jobs.stop(
        StopAttemptCommand(command_id(), submission.attempt.attempt_id)
    )
    root = application.settings.get().data_root
    run_directory = (
        root
        / "projects"
        / str(project.project_id)
        / "runs"
        / str(submission.run.run_id)
    )
    run_directory.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-run-owner"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    run_directory.symlink_to(outside, target_is_directory=True)

    with TestClient(
        create_app(application=application, run_dispatcher=CapturingDispatcher())
    ) as client:
        response = client.delete(
            f"/api/runs/{submission.run.run_id}",
            headers={"Idempotency-Key": command_id()},
        )

    assert response.status_code == 200
    assert not run_directory.exists()
    assert sentinel.read_text(encoding="utf-8") == "untouched"
