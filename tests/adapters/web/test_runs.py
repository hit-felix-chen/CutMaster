from __future__ import annotations

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
from cutmaster.application.projects import (
    CreateProjectCommand,
    SaveCreativeBriefCommand,
    SetProjectMaterialsCommand,
)
from cutmaster.application.jobs import (
    ClaimJobCommand,
    HeartbeatJobCommand,
)
from cutmaster.application.runs import CreateRunCommand, RunSubmissionView
from cutmaster.domain.runs import RunStatus
from cutmaster.workflow.ports import ProgressUpdate


def command_id() -> str:
    return str(uuid4())


class CapturingDispatcher:
    def __init__(self) -> None:
        self.submissions: list[RunSubmissionView] = []

    def dispatch(self, submission: RunSubmissionView) -> None:
        self.submissions.append(submission)


def prepared_project(application: CutMasterApplication, tmp_path: Path):
    project = application.projects.create(
        CreateProjectCommand(command_id(), "Web Run")
    )
    video_path = tmp_path / f"{uuid4()}.mp4"
    music_path = tmp_path / f"{uuid4()}.mp3"
    video_path.write_bytes(b"video")
    music_path.write_bytes(b"music")
    video = application.materials.add(video_path, "video", f"Video {uuid4()}")
    music = application.materials.add(music_path, "music", f"Music {uuid4()}")
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
            45,
        )
    )


def fake_planner(_application, run, workspace: Path) -> Path:
    assert run.creative_brief.editing_intent == "Build a tense reunion"
    assert run.creative_brief.target_duration_sec == 45
    path = workspace / "render_plan.json"
    path.write_bytes(b'{"render_plan":"real-worker-artifact"}\n')
    return path


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
    application.jobs.heartbeat(
        HeartbeatJobCommand(submission.job.job_id, progress)
    )

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
        "run_id": str(submission.run.run_id),
    }


def test_project_run_list_does_not_access_materials(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    submission = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )

    with patch.object(
        CutMasterApplication,
        "materials",
        new_callable=PropertyMock,
        side_effect=AssertionError("The lightweight Run list accessed Materials"),
    ):
        with TestClient(
            create_app(
                application=application,
                run_dispatcher=CapturingDispatcher(),
            )
        ) as client:
            response = client.get(f"/api/projects/{project.project_id}/runs")

    assert response.status_code == 200
    assert response.json()["items"][0]["run"]["run_id"] == str(
        submission.run.run_id
    )


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


def test_run_worker_claims_the_dispatched_job_instead_of_an_older_job(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project = prepared_project(application, tmp_path)
    older = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )
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
