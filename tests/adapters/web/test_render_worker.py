from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from cutmaster.adapters.web import create_app
from cutmaster.adapters.web.render_worker import execute_render_job
from cutmaster.adapters.web.run_worker import execute_run_job
from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import (
    ClaimJobCommand,
    FailAttemptCommand,
    StopAttemptCommand,
)
from cutmaster.application.projects import (
    CreateProjectCommand,
    SaveCreativeBriefCommand,
    SetProjectMaterialsCommand,
)
from cutmaster.application.renders import (
    CreateRenderVariantCommand,
    DeleteRenderVariantCommand,
    RecoverRenderVariantCommand,
    VerifyRenderVariantCommand,
)
from cutmaster.application.renders.service import RendersService
from cutmaster.application.runs import CompleteRunCommand, CreateRunCommand
from cutmaster.domain.attempts import AttemptStatus
from cutmaster.domain.renders import RenderVariantStatus
from cutmaster.workflow.contracts.render_plan import RenderPlan
from cutmaster.workflow.contracts.rendering import RenderResult


def command_id() -> str:
    return str(uuid4())


def _ready_edit(application: CutMasterApplication, tmp_path: Path):
    project = application.projects.create(
        CreateProjectCommand(command_id(), "Managed Renderer")
    )
    identities = {}
    for material_type, suffix in (("video", ".mp4"), ("music", ".mp3")):
        source = tmp_path / f"{material_type}{suffix}"
        source.write_bytes(material_type.encode())
        material = application.materials.add(
            source,
            material_type,
            f"Render {material_type}",
        )
        with application.materials.lease(material.material_id) as binding:
            identities[material_type] = binding.material
            if material_type == "music":
                (binding.memory_root / "music_memory.json").write_text(
                    json.dumps({"source_duration_sec": 60.0}),
                    encoding="utf-8",
                )
            staged = tmp_path / f"{material_type}-result.json"
            staged.write_text(
                json.dumps(
                    {
                        "schema_version": "2.0",
                        "status": "success",
                        "material_id": str(binding.material.material_id),
                        "material_type": material_type,
                        "material_name": binding.material.name,
                        "material_fingerprint": str(binding.material.fingerprint),
                    }
                ),
                encoding="utf-8",
            )
            application.materials.publish_analysis_result(binding, staged)
    video = identities["video"]
    music = identities["music"]
    application.projects.set_materials(
        SetProjectMaterialsCommand(
            command_id(),
            project.project_id,
            (video.material_id,),
            (music.material_id,),
        )
    )
    application.projects.save_creative_brief(
        SaveCreativeBriefCommand(
            command_id(),
            project.project_id,
            "Render the coherent scene",
            1.0,
        )
    )
    run = application.runs.create(CreateRunCommand(command_id(), project.project_id))
    assert application.jobs.claim_next(
        ClaimJobCommand("renderer-plan-worker", 111, run.job.job_id)
    ) is not None
    relative_plan = f"projects/{project.project_id}/runs/{run.run.run_id}/plan.json"
    plan = RenderPlan.create(
        video_material_id=video.material_id,
        video_expected_fingerprint=video.fingerprint,
        music_material_id=music.material_id,
        music_expected_fingerprint=music.fingerprint,
        fps=30,
        clips=[
            {
                "timestamp": "00:00:00,000-00:00:01,000",
                "output_frame_range": [0, 30],
            }
        ],
        planners_metadata={"test": True},
    )
    plan.write(application.settings.get().data_root / relative_plan)
    completed = application.runs.complete(
        CompleteRunCommand(
            command_id(),
            run.run.run_id,
            run.attempt.attempt_id,
            relative_plan,
        )
    )
    return project, completed.frozen_edit, plan


class FakeRenderer:
    def __init__(self, config, *, on_render=None) -> None:
        self.config = config
        self.on_render = on_render

    def render(self, request, *, overwrite=False) -> RenderResult:
        assert not overwrite
        assert request.plan.fps == self.config.fps
        output = request.output_target.root / request.output_target.output_filename
        montage = request.output_target.root / "montage.mp4"
        output.write_bytes(b"managed-render-master")
        montage.write_bytes(b"managed-montage")
        if self.on_render is not None:
            self.on_render()
        return RenderResult(
            status="success",
            plan_id=request.plan.plan_id,
            render_id="fake-render",
            audio_mode=request.options.audio_mode,
            output_video=output,
            montage_video=montage,
            duration_sec=request.plan.duration_sec,
            frames=request.plan.total_frames,
            montage_reused=False,
            dialogue_audio_reused=False,
            stage_timings_sec={"rendering": 0.01},
            wall_clock_sec=0.01,
        )


def test_render_worker_executes_real_managed_boundary_and_publishes_atomically(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project, edit, plan = _ready_edit(application, tmp_path)
    submission = application.renders.create(
        CreateRenderVariantCommand(command_id(), edit.edit_id, "bgm_only")
    )

    result = execute_render_job(
        application,
        submission.job.job_id,
        renderer_factory=FakeRenderer,
        heartbeat_interval_sec=0.01,
    )

    assert result is AttemptStatus.COMPLETE
    variant = application.renders.get(submission.render_variant.render_variant_id)
    assert variant.status is RenderVariantStatus.READY
    assert variant.frame_count == plan.total_frames
    master = (
        application.settings.get().data_root
        / "projects"
        / str(project.project_id)
        / "renders"
        / str(variant.render_variant_id)
        / "master.mp4"
    )
    assert master.read_bytes() == b"managed-render-master"
    assert not list(master.parent.glob(".attempt-*"))


def test_render_stop_waits_for_media_operation_then_interrupts_before_publish(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project, edit, _plan = _ready_edit(application, tmp_path)
    submission = application.renders.create(
        CreateRenderVariantCommand(command_id(), edit.edit_id, "dialogue")
    )

    def request_stop() -> None:
        application.jobs.stop(
            StopAttemptCommand(command_id(), submission.attempt.attempt_id)
        )

    result = execute_render_job(
        application,
        submission.job.job_id,
        renderer_factory=lambda config: FakeRenderer(
            config,
            on_render=request_stop,
        ),
        heartbeat_interval_sec=0.01,
    )

    assert result is AttemptStatus.INTERRUPTED
    assert (
        application.renders.get(submission.render_variant.render_variant_id).status
        is RenderVariantStatus.INTERRUPTED
    )
    master = (
        application.settings.get().data_root
        / "projects"
        / str(project.project_id)
        / "renders"
        / str(submission.render_variant.render_variant_id)
        / "master.mp4"
    )
    assert not master.exists()


def test_render_delete_unlinks_owned_directory_symlink_without_following_it(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project, edit, _plan = _ready_edit(application, tmp_path)
    submission = application.renders.create(
        CreateRenderVariantCommand(command_id(), edit.edit_id, "bgm_only")
    )
    assert execute_render_job(
        application,
        submission.job.job_id,
        renderer_factory=FakeRenderer,
        heartbeat_interval_sec=0.01,
    ) is AttemptStatus.COMPLETE

    owner = (
        application.settings.get().data_root
        / "projects"
        / str(project.project_id)
        / "renders"
        / str(submission.render_variant.render_variant_id)
    )
    outside = tmp_path / "outside-render-owner"
    owner.replace(outside)
    owner.symlink_to(outside, target_is_directory=True)

    application.renders.delete(
        DeleteRenderVariantCommand(
            command_id(),
            submission.render_variant.render_variant_id,
        )
    )

    assert not owner.is_symlink()
    assert (outside / "master.mp4").read_bytes() == b"managed-render-master"


def test_render_media_lease_pins_verified_descriptor_across_cascade_unlink(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    _project, edit, _plan = _ready_edit(application, tmp_path)
    submission = application.renders.create(
        CreateRenderVariantCommand(command_id(), edit.edit_id, "bgm_only")
    )
    assert execute_render_job(
        application,
        submission.job.job_id,
        renderer_factory=FakeRenderer,
        heartbeat_interval_sec=0.01,
    ) is AttemptStatus.COMPLETE

    lease = application.renders.acquire_media(
        submission.render_variant.render_variant_id
    )
    path, stream, _observed = lease.acquire()
    try:
        path.unlink()
        assert stream.read() == b"managed-render-master"
    finally:
        lease.release()


def test_render_publication_failure_restores_previous_owned_master(
    application: CutMasterApplication,
    tmp_path: Path,
    monkeypatch,
) -> None:
    project, edit, _plan = _ready_edit(application, tmp_path)
    initial = application.renders.create(
        CreateRenderVariantCommand(command_id(), edit.edit_id, "bgm_only")
    )
    assert execute_render_job(
        application,
        initial.job.job_id,
        renderer_factory=FakeRenderer,
        heartbeat_interval_sec=0.01,
    ) is AttemptStatus.COMPLETE
    master = (
        application.settings.get().data_root
        / "projects"
        / str(project.project_id)
        / "renders"
        / str(initial.render_variant.render_variant_id)
        / "master.mp4"
    )
    previous = b"previous-owned-master"
    master.write_bytes(previous)
    assert application.renders.verify(
        VerifyRenderVariantCommand(
            command_id(),
            initial.render_variant.render_variant_id,
        )
    ).status is RenderVariantStatus.UNAVAILABLE
    replacement = application.renders.render_again(
        RecoverRenderVariantCommand(
            command_id(),
            initial.render_variant.render_variant_id,
        )
    )

    def fail_commit(self, command):
        raise RuntimeError("database commit failed")

    monkeypatch.setattr(RendersService, "complete", fail_commit)
    assert execute_render_job(
        application,
        replacement.job.job_id,
        renderer_factory=FakeRenderer,
        heartbeat_interval_sec=0.01,
    ) is AttemptStatus.FAILED
    assert master.read_bytes() == previous


def test_run_completion_creates_and_dispatches_one_default_dialogue_preview(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project, existing_edit, _ = _ready_edit(application, tmp_path)
    # Build another Run so the test exercises the Run worker completion edge.
    submission = application.runs.create(
        CreateRunCommand(command_id(), project.project_id)
    )

    def planner(app, run, workspace: Path) -> Path:
        source = app.runs.get_frozen_edit(existing_edit.edit_id)
        source_plan = app.settings.get().data_root / source.plan.relative_path
        destination = workspace / "plan.json"
        destination.write_bytes(source_plan.read_bytes())
        return destination

    previews = []
    result = execute_run_job(
        application,
        submission.job.job_id,
        planner=planner,
        preview_dispatcher=previews.append,
        heartbeat_interval_sec=0.01,
    )

    assert result.value == "complete"
    created_edit = application.runs.list_frozen_edits(submission.run.run_id)[0]
    variants = application.renders.list(created_edit.edit_id)
    assert len(variants) == 1
    assert variants[0].specification["audio_mode"] == "dialogue"
    assert len(previews) == 1


class CapturingRenderDispatcher:
    def __init__(self) -> None:
        self.submissions = []

    def dispatch(self, submission) -> None:
        self.submissions.append(submission)


def _headers(command: str | None = None) -> dict[str, str]:
    return {"Idempotency-Key": command or command_id()}


def test_render_variant_http_contract_lists_verifies_downloads_and_deletes(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project, edit, _plan = _ready_edit(application, tmp_path)
    dispatcher = CapturingRenderDispatcher()
    with TestClient(
        create_app(application=application, render_dispatcher=dispatcher)
    ) as client:
        created = client.post(
            f"/api/frozen-edits/{edit.edit_id}/render-variants",
            headers=_headers(),
            json={"audio_mode": "bgm_only"},
        )

        assert created.status_code == 202
        payload = created.json()
        assert payload["created"] is True
        assert payload["execution"]["attempt"]["status"] == "queued"
        variant = payload["render_variant"]
        render_id = variant["render_variant_id"]
        assert variant["project_id"] == str(project.project_id)
        assert variant["run_id"] == str(edit.run_id)
        assert variant["edit_id"] == str(edit.edit_id)
        assert variant["edit_origin"] == "initial"
        assert variant["status"] == "queued"
        assert variant["media_url"] is None
        assert variant["download_url"] is None
        assert set(variant["specification"]) == {
            "schema_version",
            "audio_mode",
            "renderer",
        }
        assert not {
            "master_sha256",
            "specification_digest",
            "master_relative_path",
        } & set(variant)
        assert len(dispatcher.submissions) == 1

        duplicate = client.post(
            f"/api/frozen-edits/{edit.edit_id}/render-variants",
            headers=_headers(),
            json={"audio_mode": "bgm_only"},
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["created"] is False
        assert duplicate.json()["render_variant"]["render_variant_id"] == render_id
        assert len(dispatcher.submissions) == 1

        submission = dispatcher.submissions[0]
        assert execute_render_job(
            application,
            submission.job.job_id,
            renderer_factory=FakeRenderer,
            heartbeat_interval_sec=0.01,
        ) is AttemptStatus.COMPLETE

        detail = client.get(f"/api/render-variants/{render_id}")
        by_edit = client.get(
            f"/api/frozen-edits/{edit.edit_id}/render-variants"
        )
        by_project = client.get(
            f"/api/projects/{project.project_id}/render-variants"
        )
        assert detail.status_code == by_edit.status_code == by_project.status_code == 200
        ready = detail.json()["render_variant"]
        assert ready["status"] == "ready"
        assert ready["media_url"] == f"/api/render-variants/{render_id}/media"
        assert ready["download_url"] == f"/api/render-variants/{render_id}/download"
        assert by_edit.json()["items"][0]["render_variant"] == ready
        assert by_project.json()["items"][0]["render_variant"] == ready

        verification_key = command_id()
        verified = client.post(
            f"/api/render-variants/{render_id}/verify",
            headers=_headers(verification_key),
        )
        replayed = client.post(
            f"/api/render-variants/{render_id}/verify",
            headers=_headers(verification_key),
        )
        assert verified.status_code == replayed.status_code == 200
        assert verified.json() == replayed.json()
        assert set(verified.json()["integrity"]) == {
            "state",
            "cached",
            "size_bytes",
        }

        downloaded = client.get(f"/api/render-variants/{render_id}/download")
        assert downloaded.status_code == 200
        assert downloaded.content == b"managed-render-master"
        assert downloaded.headers["content-disposition"].startswith("attachment;")

        root = application.settings.get().data_root
        job_log = root / "logs" / "jobs" / f"{submission.job.job_id}.log"
        job_log.parent.mkdir(parents=True, exist_ok=True)
        job_log.write_text("owned", encoding="utf-8")
        unrelated = job_log.with_name("unrelated.log")
        unrelated.write_text("keep", encoding="utf-8")
        delete_key = command_id()
        deleted = client.delete(
            f"/api/render-variants/{render_id}",
            headers=_headers(delete_key),
        )
        deleted_replay = client.delete(
            f"/api/render-variants/{render_id}",
            headers=_headers(delete_key),
        )
        assert deleted.status_code == deleted_replay.status_code == 200
        assert deleted.json() == deleted_replay.json() == {
            "render_variant_id": render_id,
            "deleted": True,
        }
        assert not job_log.exists()
        assert unrelated.read_text(encoding="utf-8") == "keep"
        assert client.get(f"/api/render-variants/{render_id}").status_code == 404


def test_render_recovery_http_contract_and_integrity_failure_are_durable(
    application: CutMasterApplication,
    tmp_path: Path,
) -> None:
    project, edit, _plan = _ready_edit(application, tmp_path)
    dispatcher = CapturingRenderDispatcher()
    with TestClient(
        create_app(application=application, render_dispatcher=dispatcher)
    ) as client:
        created = client.post(
            f"/api/frozen-edits/{edit.edit_id}/render-variants",
            headers=_headers(),
            json={"audio_mode": "dialogue"},
        )
        render_id = created.json()["render_variant"]["render_variant_id"]
        first = dispatcher.submissions[-1]
        claimed = application.jobs.claim_next(
            ClaimJobCommand("failed-renderer", 991, first.job.job_id)
        )
        assert claimed is not None
        application.jobs.mark_failed(
            FailAttemptCommand(first.attempt.attempt_id, "render failed")
        )

        retried = client.post(
            f"/api/render-variants/{render_id}/retry",
            headers=_headers(),
        )
        assert retried.status_code == 202
        retry_attempt = dispatcher.submissions[-1].attempt
        stopped = client.post(
            f"/api/attempts/{retry_attempt.attempt_id}/stop",
            headers=_headers(),
        )
        assert stopped.status_code == 200
        assert stopped.json()["attempt"]["status"] == "interrupted"

        resumed = client.post(
            f"/api/render-variants/{render_id}/resume",
            headers=_headers(),
        )
        assert resumed.status_code == 202
        resume_submission = dispatcher.submissions[-1]
        assert execute_render_job(
            application,
            resume_submission.job.job_id,
            renderer_factory=FakeRenderer,
            heartbeat_interval_sec=0.01,
        ) is AttemptStatus.COMPLETE

        variant = application.renders.get(
            resume_submission.render_variant.render_variant_id
        )
        master = application.settings.get().data_root / variant.master.relative_path
        original_size = master.stat().st_size
        master.write_bytes(b"x" * original_size)
        verify_key = command_id()
        damaged = client.post(
            f"/api/render-variants/{render_id}/verify",
            headers=_headers(verify_key),
        )
        damaged_replay = client.post(
            f"/api/render-variants/{render_id}/verify",
            headers=_headers(verify_key),
        )
        assert damaged.status_code == damaged_replay.status_code == 409
        assert damaged.json()["code"] == "render_integrity_mismatch"
        assert damaged_replay.json()["code"] == "render_integrity_mismatch"
        assert application.renders.get(variant.render_variant_id).status is RenderVariantStatus.UNAVAILABLE

        rendered_again = client.post(
            f"/api/render-variants/{render_id}/render-again",
            headers=_headers(),
        )
        assert rendered_again.status_code == 202
        again_submission = dispatcher.submissions[-1]
        assert execute_render_job(
            application,
            again_submission.job.job_id,
            renderer_factory=FakeRenderer,
            heartbeat_interval_sec=0.01,
        ) is AttemptStatus.COMPLETE
        assert application.renders.get(variant.render_variant_id).status is RenderVariantStatus.READY
