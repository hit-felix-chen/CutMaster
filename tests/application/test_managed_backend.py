from __future__ import annotations

import sqlite3
from threading import Event, Thread
from pathlib import Path
from uuid import uuid4

import pytest

from cutmaster import CutMasterApplication
from cutmaster.application.jobs import (
    ClaimJobCommand,
    FailAttemptCommand,
    JobsService,
    StopAttemptCommand,
)
from cutmaster.application.materials import MaterialsService
from cutmaster.application.projects import (
    CreateProjectCommand,
    DeleteProjectCommand,
    ProjectsService,
    SaveCreativeBriefCommand,
    SetProjectMaterialsCommand,
)
from cutmaster.application.renders import (
    CompleteRenderVariantCommand,
    CreateRenderVariantCommand,
    RendersService,
    VerifyRenderVariantCommand,
)
from cutmaster.application.runs import (
    CompleteRunCommand,
    CreateRunCommand,
    DeleteRunCommand,
    RunsService,
)
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.domain.attempts import AttemptStatus
from cutmaster.domain.ids import MaterialId
from cutmaster.domain.renders import RenderVariantStatus
from cutmaster.domain.runs import RunStatus
from cutmaster.infrastructure.persistence.sqlite import (
    ActiveAttemptBlocker,
    IdempotencyConflict,
    SQLiteApplicationStore,
)
from cutmaster.infrastructure.storage.local.material_catalog import (
    MaterialCatalog,
    MaterialNotFoundError,
    MaterialReferencedError,
)


def command_id() -> str:
    return str(uuid4())


def create_ready_project(
    configuration: EffectiveConfiguration,
    store: SQLiteApplicationStore,
):
    catalog = MaterialCatalog(
        configuration.data_root / "media",
        reference_checker=store,
    )
    materials = MaterialsService(configuration, catalog=catalog)
    projects = ProjectsService(configuration, store, materials=materials)
    project = projects.create(CreateProjectCommand(command_id(), "Film"))
    source_root = configuration.data_root.parent / "managed-test-sources"
    source_root.mkdir(exist_ok=True)
    token = str(uuid4())
    video_source = source_root / f"{token}.mp4"
    music_source = source_root / f"{token}.mp3"
    video_source.write_bytes(b"test video")
    music_source.write_bytes(b"test music")
    video = materials.add(video_source, "video", f"Video {token}").material_id
    music = materials.add(music_source, "music", f"Music {token}").material_id
    project = projects.set_materials(
        SetProjectMaterialsCommand(
            command_id(),
            project.project_id,
            (video,),
            (music,),
        )
    )
    project = projects.save_creative_brief(
        SaveCreativeBriefCommand(
            command_id(),
            project.project_id,
            "Build a coherent character arc",
            60,
        )
    )
    return projects, project, video, music


def test_project_material_assignment_requires_real_type_correct_materials(
    managed_configuration: EffectiveConfiguration,
) -> None:
    store = SQLiteApplicationStore(managed_configuration.data_root)
    catalog = MaterialCatalog(
        managed_configuration.data_root / "media",
        reference_checker=store,
    )
    materials = MaterialsService(managed_configuration, catalog=catalog)
    projects = ProjectsService(
        managed_configuration,
        store,
        materials=materials,
    )
    project = projects.create(CreateProjectCommand(command_id(), "Validation"))
    source = managed_configuration.data_root.parent / "validation.mp3"
    source.write_bytes(b"music")
    music = materials.add(source, "music", "Validation Music")

    with pytest.raises(MaterialNotFoundError):
        projects.set_materials(
            SetProjectMaterialsCommand(
                command_id(),
                project.project_id,
                (MaterialId.new(),),
                (),
            )
        )
    with pytest.raises(ValueError, match="music, expected video"):
        projects.set_materials(
            SetProjectMaterialsCommand(
                command_id(),
                project.project_id,
                (music.material_id,),
                (),
            )
        )
    assert projects.get(project.project_id).video_material_ids == ()


def test_application_composition_never_skips_project_material_validation(
    managed_configuration: EffectiveConfiguration,
) -> None:
    app = CutMasterApplication.open(managed_configuration.sources.base_path)
    project = app.projects.create(CreateProjectCommand(command_id(), "Composed"))
    source = managed_configuration.data_root.parent / "composed.mp3"
    source.write_bytes(b"music")
    music = app.materials.add(source, "music", "Composed Music")

    with pytest.raises(ValueError, match="music, expected video"):
        app.projects.set_materials(
            SetProjectMaterialsCommand(
                command_id(),
                project.project_id,
                (music.material_id,),
                (),
            )
        )
    updated = app.projects.set_materials(
        SetProjectMaterialsCommand(
            command_id(),
            project.project_id,
            (),
            (music.material_id,),
        )
    )

    assert updated.music_material_ids == (music.material_id,)


def test_material_reference_commit_and_delete_are_serialized(
    managed_configuration: EffectiveConfiguration,
) -> None:
    entered_store = Event()
    release_store = Event()

    class PausingStore(SQLiteApplicationStore):
        def set_project_materials(self, *args, **kwargs):
            entered_store.set()
            assert release_store.wait(timeout=5)
            return super().set_project_materials(*args, **kwargs)

    store = PausingStore(managed_configuration.data_root)
    catalog = MaterialCatalog(
        managed_configuration.data_root / "media",
        reference_checker=store,
    )
    materials = MaterialsService(managed_configuration, catalog=catalog)
    projects = ProjectsService(
        managed_configuration,
        store,
        materials=materials,
    )
    project = projects.create(CreateProjectCommand(command_id(), "Race"))
    source = managed_configuration.data_root.parent / "race.mp4"
    source.write_bytes(b"video")
    video = materials.add(source, "video", "Race Video")
    assignment_errors: list[BaseException] = []
    deletion_errors: list[BaseException] = []

    def assign() -> None:
        try:
            projects.set_materials(
                SetProjectMaterialsCommand(
                    command_id(),
                    project.project_id,
                    (video.material_id,),
                    (),
                )
            )
        except BaseException as error:  # pragma: no cover - diagnostic capture
            assignment_errors.append(error)

    def delete() -> None:
        try:
            materials.delete(video.material_id)
        except BaseException as error:  # pragma: no cover - asserted below
            deletion_errors.append(error)

    assignment = Thread(target=assign)
    assignment.start()
    assert entered_store.wait(timeout=5)
    deletion = Thread(target=delete)
    deletion.start()
    deletion.join(timeout=0.1)
    assert deletion.is_alive(), "delete bypassed the active Project Material lease"

    release_store.set()
    assignment.join(timeout=5)
    deletion.join(timeout=5)

    assert not assignment.is_alive()
    assert not deletion.is_alive()
    assert assignment_errors == []
    assert len(deletion_errors) == 1
    assert isinstance(deletion_errors[0], MaterialReferencedError)
    assert projects.get(project.project_id).video_material_ids == (
        video.material_id,
    )
    assert materials.get(video.material_id) is not None


def test_sqlite_schema_foreign_keys_and_idempotent_project_crud(
    managed_configuration: EffectiveConfiguration,
) -> None:
    store = SQLiteApplicationStore(managed_configuration.data_root)
    projects = ProjectsService(managed_configuration, store)
    create_id = command_id()
    command = CreateProjectCommand(create_id, "Project A")

    first = projects.create(command)
    replay = projects.create(command)

    assert first == replay
    assert projects.list() == (first,)
    with pytest.raises(IdempotencyConflict):
        projects.create(CreateProjectCommand(create_id, "Different"))
    assert store.schema_version == 1
    assert store.integrity_check() == "ok"
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "run_00000000-0000-4000-8000-000000000001",
                    "project_00000000-0000-4000-8000-000000000099",
                    1,
                    "queued",
                    "intent",
                    1.0,
                    "{}",
                    None,
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
            )


def test_run_snapshot_reference_blocking_and_cascade_delete(
    managed_configuration: EffectiveConfiguration,
) -> None:
    store = SQLiteApplicationStore(managed_configuration.data_root)
    projects, project, video, music = create_ready_project(
        managed_configuration,
        store,
    )
    runs = RunsService(managed_configuration, store)
    jobs = JobsService(managed_configuration, store)
    submission = runs.create(CreateRunCommand(command_id(), project.project_id))

    assert submission.run.status is RunStatus.QUEUED
    assert set(store.references(video)) == {
        f"project:{project.project_id}:current:video",
        f"run:{submission.run.run_id}:snapshot:video",
    }
    projects.set_materials(
        SetProjectMaterialsCommand(command_id(), project.project_id, (), ())
    )
    assert store.references(video) == (
        f"run:{submission.run.run_id}:snapshot:video",
    )

    with pytest.raises(ActiveAttemptBlocker):
        runs.delete(DeleteRunCommand(command_id(), submission.run.run_id))
    stopped = jobs.stop(StopAttemptCommand(command_id(), submission.attempt.attempt_id))
    assert stopped.attempt.status is AttemptStatus.INTERRUPTED
    assert runs.get(submission.run.run_id).status is RunStatus.INTERRUPTED
    deleted = runs.delete(DeleteRunCommand(command_id(), submission.run.run_id))
    assert deleted.deleted
    assert store.references(video) == ()
    assert store.references(music) == ()


def test_job_claim_failed_and_interrupted_are_distinct(
    managed_configuration: EffectiveConfiguration,
) -> None:
    store = SQLiteApplicationStore(managed_configuration.data_root)
    _projects, project, _video, _music = create_ready_project(
        managed_configuration,
        store,
    )
    runs = RunsService(managed_configuration, store)
    jobs = JobsService(managed_configuration, store)

    failed_run = runs.create(CreateRunCommand(command_id(), project.project_id))
    claimed = jobs.claim_next(ClaimJobCommand("worker-a", 1234))
    assert claimed is not None
    assert claimed.attempt.attempt_id == failed_run.attempt.attempt_id
    failed = jobs.mark_failed(
        FailAttemptCommand(failed_run.attempt.attempt_id, "provider error")
    )
    assert failed.attempt.status is AttemptStatus.FAILED
    assert runs.get(failed_run.run.run_id).status is RunStatus.FAILED

    interrupted_run = runs.create(CreateRunCommand(command_id(), project.project_id))
    claimed = jobs.claim_next(ClaimJobCommand("worker-a", 1235))
    assert claimed is not None
    stopping = jobs.stop(
        StopAttemptCommand(command_id(), interrupted_run.attempt.attempt_id)
    )
    assert stopping.attempt.status is AttemptStatus.STOPPING
    interrupted = jobs.mark_interrupted(interrupted_run.attempt.attempt_id)
    assert interrupted.attempt.status is AttemptStatus.INTERRUPTED
    assert runs.get(interrupted_run.run.run_id).status is RunStatus.INTERRUPTED
    assert {item.event_type for item in jobs.events()} >= {
        "attempt.failed",
        "attempt.stop_requested",
        "attempt.interrupted",
    }


def test_job_can_be_claimed_by_exact_identifier_without_consuming_older_work(
    managed_configuration: EffectiveConfiguration,
) -> None:
    store = SQLiteApplicationStore(managed_configuration.data_root)
    _projects, project, _video, _music = create_ready_project(
        managed_configuration,
        store,
    )
    runs = RunsService(managed_configuration, store)
    jobs = JobsService(managed_configuration, store)
    older = runs.create(CreateRunCommand(command_id(), project.project_id))
    selected = runs.create(CreateRunCommand(command_id(), project.project_id))

    claimed = jobs.claim_next(
        ClaimJobCommand("exact-worker", 4321, selected.job.job_id)
    )

    assert claimed is not None
    assert claimed.job.job_id == selected.job.job_id
    assert runs.get(selected.run.run_id).status is RunStatus.PLANNERS
    assert runs.get(older.run.run_id).status is RunStatus.QUEUED


def test_run_completion_render_integrity_and_unavailable_transition(
    managed_configuration: EffectiveConfiguration,
) -> None:
    store = SQLiteApplicationStore(managed_configuration.data_root)
    _projects, project, _video, _music = create_ready_project(
        managed_configuration,
        store,
    )
    runs = RunsService(managed_configuration, store)
    jobs = JobsService(managed_configuration, store)
    renders = RendersService(managed_configuration, store)

    run = runs.create(CreateRunCommand(command_id(), project.project_id))
    assert jobs.claim_next(ClaimJobCommand("worker", 123)) is not None
    completed_run = runs.complete(
        CompleteRunCommand(
            command_id(),
            run.run.run_id,
            run.attempt.attempt_id,
            f"projects/{project.project_id}/runs/{run.run.run_id}/plan.json",
        )
    )
    assert completed_run.run.status is RunStatus.COMPLETE

    render = renders.create(
        CreateRenderVariantCommand(
            command_id(),
            completed_run.frozen_edit.edit_id,
            {"audio_mode": "dialogue", "fps": 30},
        )
    )
    duplicate = renders.create(
        CreateRenderVariantCommand(
            command_id(),
            completed_run.frozen_edit.edit_id,
            {"fps": 30, "audio_mode": "dialogue"},
        )
    )
    assert not duplicate.created
    assert duplicate.render_variant.render_variant_id == render.render_variant.render_variant_id
    assert jobs.claim_next(ClaimJobCommand("worker", 124)) is not None

    master_relative = (
        f"projects/{project.project_id}/renders/"
        f"{render.render_variant.render_variant_id}/master.mp4"
    )
    master = managed_configuration.data_root / master_relative
    master.parent.mkdir(parents=True)
    master.write_bytes(b"real-render-bytes")
    ready = renders.complete(
        CompleteRenderVariantCommand(
            command_id(),
            render.render_variant.render_variant_id,
            render.attempt.attempt_id,
            master_relative,
            frame_count=120,
            duration_sec=4.0,
        )
    )
    assert ready.render_variant.status is RenderVariantStatus.READY
    master.write_bytes(b"tampered")
    unavailable = renders.verify(
        VerifyRenderVariantCommand(
            command_id(),
            render.render_variant.render_variant_id,
        )
    )
    assert unavailable.status is RenderVariantStatus.UNAVAILABLE


def test_project_delete_cascades_terminal_run_history(
    managed_configuration: EffectiveConfiguration,
) -> None:
    store = SQLiteApplicationStore(managed_configuration.data_root)
    projects, project, _video, _music = create_ready_project(
        managed_configuration,
        store,
    )
    runs = RunsService(managed_configuration, store)
    jobs = JobsService(managed_configuration, store)
    run = runs.create(CreateRunCommand(command_id(), project.project_id))
    jobs.stop(StopAttemptCommand(command_id(), run.attempt.attempt_id))

    deleted = projects.delete(DeleteProjectCommand(command_id(), project.project_id))

    assert deleted.deleted
    assert projects.list() == ()
    with sqlite3.connect(store.database_path) as connection:
        for table in ("projects", "runs", "frozen_edits", "attempts", "jobs"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
