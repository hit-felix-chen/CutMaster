from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import Event, Thread
from uuid import uuid4

import pytest

from cutmaster import CutMasterApplication
from cutmaster.application.jobs import (
    ClaimJobCommand,
    FailAttemptCommand,
    JobsService,
    RecordAttemptUsageCommand,
    StopAttemptCommand,
)
from cutmaster.application.jobs.usage import normalize_usage_summary
from cutmaster.application.materials import MaterialsService
from cutmaster.application.projects import (
    CreateProjectCommand,
    DeleteProjectCommand,
    EnsureProjectByNameCommand,
    ProjectsService,
    RenameProjectCommand,
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
    RecoverRunCommand,
    RunsService,
)
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.domain.attempts import AttemptStatus
from cutmaster.domain.ids import MaterialId
from cutmaster.domain.renders import RenderVariantStatus
from cutmaster.domain.materials import MaterialFingerprint
from cutmaster.domain.runs import RunStatus
from cutmaster.infrastructure.persistence.sqlite import (
    ActiveAttemptBlocker,
    IdempotencyConflict,
    ManagedStateConflict,
    ProjectNameConflict,
    SQLiteApplicationStore,
    SQLiteStoreError,
)
from cutmaster.infrastructure.storage.local.material_catalog import (
    MaterialCatalog,
    MaterialConsumedError,
    MaterialNotFoundError,
    MaterialReferencedError,
)
from cutmaster.workflow.contracts.render_plan import RenderPlan


def command_id() -> str:
    return str(uuid4())


def model_usage_summary(
    *,
    model: str = "qwen-max",
    task: str = "story_editor",
    total_tokens: int = 100,
    total_cost_yuan: float = 0.01,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "currency": "CNY",
        "price_unit": "yuan_per_million_tokens",
        "request_count": 1,
        "reported_usage_count": 1,
        "unreported_usage_count": 0,
        "priced_usage_count": 1,
        "unpriced_usage_count": 0,
        "prompt_tokens": total_tokens - 10,
        "completion_tokens": 10,
        "total_tokens": total_tokens,
        "cached_prompt_tokens": 20,
        "uncached_prompt_tokens": total_tokens - 30,
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
    # These fields may exist in a workflow artifact but never enter managed state.
    summary["calls"] = [{"response_id": "secret-response"}]
    summary["provider_usage"] = {"opaque": True}
    summary["artifact"] = "/private/model_usage.json"
    return summary


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
    for material_id in (video, music):
        with materials.lease(material_id) as binding:
            if binding.material.material_type.value == "music":
                (binding.memory_root / "music_memory.json").write_text(
                    json.dumps(
                        {
                            "schema_version": "2.0",
                            "source_duration_sec": 120.0,
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
            staged = source_root / f"{material_id}-analysis-result.json"
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
            materials.publish_analysis_result(binding, staged)
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


def publish_ready_material(
    materials: MaterialsService,
    material_id: MaterialId,
    directory: Path,
) -> None:
    with materials.lease(material_id) as binding:
        if binding.material.material_type.value == "music":
            (binding.memory_root / "music_memory.json").write_text(
                json.dumps({"schema_version": "2.0", "source_duration_sec": 120.0}),
                encoding="utf-8",
            )
        payload = {
            "schema_version": "3.0",
            "status": "success",
            "material_id": str(binding.material.material_id),
            "material_type": binding.material.material_type.value,
            "material_name": binding.material.name,
            "material_fingerprint": str(binding.material.fingerprint),
            "memory_schema_version": (
                "3.0" if binding.material.material_type.value == "video" else "2.0"
            ),
            "elapsed_sec": 0.0,
            "material_reused": False,
            "analysis_reused": False,
        }
        if binding.material.material_type.value == "video":
            payload["model_usage_summary"] = {}
            payload["model_usage_cumulative_summary"] = {}
        staged = directory / f"{binding.material.material_id}-ready.json"
        staged.write_text(json.dumps(payload), encoding="utf-8")
        materials.publish_analysis_result(binding, staged)


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
    with pytest.raises(ManagedStateConflict) as not_ready:
        projects.set_materials(
            SetProjectMaterialsCommand(
                command_id(),
                project.project_id,
                (),
                (music.material_id,),
            )
        )
    assert not_ready.value.code == "project_material_not_ready"
    assert projects.get(project.project_id).video_material_ids == ()


def test_application_composition_never_skips_project_material_validation(
    managed_configuration: EffectiveConfiguration,
) -> None:
    app = CutMasterApplication.open(managed_configuration.sources.base_path)
    project = app.projects.create(CreateProjectCommand(command_id(), "Composed"))
    source = managed_configuration.data_root.parent / "composed.mp3"
    source.write_bytes(b"music")
    music = app.materials.add(source, "music", "Composed Music")
    publish_ready_material(
        app.materials,
        music.material_id,
        managed_configuration.data_root.parent,
    )

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
    publish_ready_material(
        materials,
        video.material_id,
        managed_configuration.data_root.parent,
    )
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
    deletion.join(timeout=1)
    assert not deletion.is_alive(), "delete waited for an active shared consumer"
    assert len(deletion_errors) == 1
    assert isinstance(deletion_errors[0], MaterialConsumedError)

    release_store.set()
    assignment.join(timeout=5)
    deletion.join(timeout=5)

    assert not assignment.is_alive()
    assert not deletion.is_alive()
    assert assignment_errors == []
    assert projects.get(project.project_id).video_material_ids == (video.material_id,)
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
    assert store.schema_version == 6
    assert store.integrity_check() == "ok"
    with sqlite3.connect(store.database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    '{"target_shot_length_sec":4.0,"prompt_type":"event"}',
                ),
            )


def test_project_names_are_unique_after_normalisation_and_case_sensitive(
    managed_configuration: EffectiveConfiguration,
) -> None:
    store = SQLiteApplicationStore(managed_configuration.data_root)
    projects = ProjectsService(managed_configuration, store)
    existing = projects.create(CreateProjectCommand(command_id(), "Shared Project"))

    with pytest.raises(ProjectNameConflict) as duplicate:
        projects.create(CreateProjectCommand(command_id(), "  Shared Project  "))

    assert duplicate.value.code == "project_name_conflict"
    assert duplicate.value.project_id == existing.project_id
    assert duplicate.value.project_name == "Shared Project"
    case_variant = projects.create(
        CreateProjectCommand(command_id(), "shared project")
    )
    assert case_variant.project_id != existing.project_id


def test_project_rename_rejects_another_projects_name(
    managed_configuration: EffectiveConfiguration,
) -> None:
    store = SQLiteApplicationStore(managed_configuration.data_root)
    projects = ProjectsService(managed_configuration, store)
    existing = projects.create(CreateProjectCommand(command_id(), "Existing"))
    renamed = projects.create(CreateProjectCommand(command_id(), "Rename Me"))

    with pytest.raises(ProjectNameConflict) as duplicate:
        projects.rename(
            RenameProjectCommand(command_id(), renamed.project_id, " Existing ")
        )

    assert duplicate.value.project_id == existing.project_id
    assert duplicate.value.project_name == "Existing"
    assert projects.get(renamed.project_id).name == "Rename Me"
    assert (
        projects.rename(
            RenameProjectCommand(command_id(), renamed.project_id, "Rename Me")
        ).project_id
        == renamed.project_id
    )


def test_ensure_project_by_name_is_atomic_and_reuses_the_existing_project(
    managed_configuration: EffectiveConfiguration,
) -> None:
    store = SQLiteApplicationStore(managed_configuration.data_root)
    projects = ProjectsService(managed_configuration, store)

    first = projects.ensure_by_name(
        EnsureProjectByNameCommand(command_id(), "  Benchmark Project  ")
    )
    replay = projects.ensure_by_name(
        EnsureProjectByNameCommand(command_id(), "Benchmark Project")
    )

    assert replay.project_id == first.project_id
    assert projects.list() == (first,)

    gate = Event()
    results = []
    errors: list[BaseException] = []

    def ensure() -> None:
        gate.wait(timeout=5)
        try:
            results.append(
                projects.ensure_by_name(
                    EnsureProjectByNameCommand(command_id(), "Concurrent Project")
                )
            )
        except BaseException as error:  # pragma: no cover - asserted below
            errors.append(error)

    threads = [Thread(target=ensure), Thread(target=ensure)]
    for thread in threads:
        thread.start()
    gate.set()
    for thread in threads:
        thread.join(timeout=5)

    assert all(not thread.is_alive() for thread in threads)
    assert errors == []
    assert len(results) == 2
    assert results[0].project_id == results[1].project_id
    assert [item.name for item in projects.list()] == [
        "Concurrent Project",
        "Benchmark Project",
    ]


def test_sqlite_migrates_v1_attempts_to_usage_and_checkpoint_schema(
    tmp_path: Path,
) -> None:
    from cutmaster.infrastructure.persistence.sqlite.migrations import MIGRATIONS

    data_root = tmp_path / "v1-data"
    data_root.mkdir()
    database_path = data_root / "cutmaster.db"
    with sqlite3.connect(database_path) as connection:
        for statement in MIGRATIONS[0].statements:
            connection.execute(statement)
        connection.execute("PRAGMA user_version = 1")
        connection.commit()

    store = SQLiteApplicationStore(data_root)

    assert store.schema_version == 6
    with sqlite3.connect(database_path) as connection:
        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(attempts)")
        }
        assert "model_usage_summary_json" in columns
        assert columns["model_usage_summary_json"][3] == 0
        assert "resume_checkpoint_json" in columns
        assert columns["resume_checkpoint_json"][3] == 0
        checkpoint_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("run_checkpoints",),
        ).fetchone()
        assert checkpoint_table == ("run_checkpoints",)
        run_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(runs)")
        }
        assert run_columns["planning_options_json"][3] == 1
        project_name_index = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("projects_name_unique_idx",),
        ).fetchone()
        assert project_name_index is not None
        assert "UNIQUE INDEX" in project_name_index[0]


def test_schema_v5_refuses_legacy_duplicate_project_names_without_mutation(
    tmp_path: Path,
) -> None:
    from cutmaster.infrastructure.persistence.sqlite.migrations import MIGRATIONS

    data_root = tmp_path / "duplicate-project-data"
    data_root.mkdir()
    database_path = data_root / "cutmaster.db"
    with sqlite3.connect(database_path) as connection:
        for migration in MIGRATIONS:
            if migration.version >= 5:
                break
            for statement in migration.statements:
                connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {migration.version}")
        connection.executemany(
            """
            INSERT INTO projects (
                project_id, name, brief_intent, brief_target_duration,
                created_at, updated_at
            ) VALUES (?, 'Duplicate', NULL, NULL, ?, ?)
            """,
            [
                (
                    "project_00000000-0000-4000-8000-000000000001",
                    "2026-01-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
                (
                    "project_00000000-0000-4000-8000-000000000002",
                    "2026-01-01T00:00:01+00:00",
                    "2026-01-01T00:00:01+00:00",
                ),
            ],
        )
        connection.commit()

    with pytest.raises(SQLiteStoreError, match="Project Name 'Duplicate'.*2 Projects"):
        SQLiteApplicationStore(data_root)

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("SELECT count(*) FROM projects").fetchone()[0] == 2
        assert connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'projects_name_unique_idx'"
        ).fetchone() is None


def test_run_snapshots_adapter_planning_options_separately_from_configuration(
    managed_configuration: EffectiveConfiguration,
) -> None:
    store = SQLiteApplicationStore(managed_configuration.data_root)
    _projects, project, _video, _music = create_ready_project(
        managed_configuration,
        store,
    )
    runs = RunsService(managed_configuration, store)

    submission = runs.create(
        CreateRunCommand(
            command_id(),
            project.project_id,
            target_shot_length_sec=2.5,
            prompt_type="narrative",
            video_title="Feature",
            max_clip_duration_sec=7.0,
        )
    )

    assert dict(submission.run.planning_options) == {
        "target_shot_length_sec": 2.5,
        "prompt_type": "narrative",
        "video_title": "Feature",
        "max_clip_duration_sec": 7.0,
    }
    assert submission.run.configuration["planners"]["aster_team"] == {
        "max_rounds": 3,
        "max_local_replans": 2,
    }
    assert "managed_request" not in submission.run.configuration["planners"]


def test_attempt_usage_is_strict_private_idempotent_and_aggregated_per_retry(
    managed_configuration: EffectiveConfiguration,
) -> None:
    store = SQLiteApplicationStore(managed_configuration.data_root)
    _projects, project, _video, _music = create_ready_project(
        managed_configuration,
        store,
    )
    runs = RunsService(managed_configuration, store)
    jobs = JobsService(managed_configuration, store)
    first = runs.create(CreateRunCommand(command_id(), project.project_id))
    assert (
        jobs.claim_next(ClaimJobCommand("usage-worker-1", 101, first.job.job_id))
        is not None
    )
    first_summary = model_usage_summary(total_tokens=100, total_cost_yuan=0.01)
    recorded = jobs.record_attempt_usage(
        RecordAttemptUsageCommand(first.attempt.attempt_id, first_summary)
    )
    replay = jobs.record_attempt_usage(
        RecordAttemptUsageCommand(first.attempt.attempt_id, first_summary)
    )
    assert replay == recorded
    assert recorded.model_usage_summary is not None
    serialized = json.dumps(dict(recorded.model_usage_summary))
    assert "calls" not in serialized
    assert "provider_usage" not in serialized
    assert "response_id" not in serialized
    assert "/private" not in serialized
    with pytest.raises(ManagedStateConflict, match="different usage summary"):
        jobs.record_attempt_usage(
            RecordAttemptUsageCommand(
                first.attempt.attempt_id,
                model_usage_summary(total_tokens=101, total_cost_yuan=0.011),
            )
        )
    jobs.mark_failed(FailAttemptCommand(first.attempt.attempt_id, "provider failed"))

    retry = runs.retry(RecoverRunCommand(command_id(), first.run.run_id))
    assert (
        jobs.claim_next(ClaimJobCommand("usage-worker-2", 102, retry.job.job_id))
        is not None
    )
    second_summary = model_usage_summary(
        model="qwen-vl-max",
        task="timeline_scout",
        total_tokens=250,
        total_cost_yuan=0.02,
    )
    completed = runs.complete(
        CompleteRunCommand(
            command_id(),
            retry.run.run_id,
            retry.attempt.attempt_id,
            f"projects/{project.project_id}/runs/{retry.run.run_id}/plan.json",
            second_summary,
        )
    )
    assert completed.run.status is RunStatus.COMPLETE
    usage = runs.usage(first.run.run_id)
    assert [item.sequence for item in usage.attempt_usage] == [1, 2]
    assert [item.status for item in usage.attempt_usage] == [
        AttemptStatus.FAILED,
        AttemptStatus.COMPLETE,
    ]
    assert usage.run_total["request_count"] == 2
    assert usage.run_total["reported_usage_count"] == 2
    assert usage.run_total["total_tokens"] == 350
    assert usage.run_total["total_cost_yuan"] == pytest.approx(0.03)
    assert set(usage.run_total["by_model"]) == {"qwen-max", "qwen-vl-max"}
    assert set(usage.run_total["by_task"]) == {"story_editor", "timeline_scout"}


def test_attempt_usage_rejects_unsafe_numeric_and_schema_values() -> None:
    mutations = (
        ("negative", lambda value: value.update(request_count=-1)),
        ("boolean", lambda value: value.update(total_tokens=True)),
        ("nonfinite", lambda value: value.update(total_cost_yuan=float("nan"))),
        ("currency", lambda value: value.update(currency="USD")),
        ("unit", lambda value: value.update(price_unit="dollars")),
        (
            "control key",
            lambda value: value.update(by_model={"bad\nmodel": value["by_model"]["qwen-max"]}),
        ),
    )
    for label, mutate in mutations:
        value = model_usage_summary()
        mutate(value)
        with pytest.raises((TypeError, ValueError)):
            normalize_usage_summary(value)



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
    assert store.references(video) == (f"run:{submission.run.run_id}:snapshot:video",)

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
    _projects, project, video, music = create_ready_project(
        managed_configuration,
        store,
    )
    runs = RunsService(managed_configuration, store)
    jobs = JobsService(managed_configuration, store)
    renders = RendersService(managed_configuration, store)

    run = runs.create(CreateRunCommand(command_id(), project.project_id))
    assert jobs.claim_next(ClaimJobCommand("worker", 123)) is not None
    relative_plan = f"projects/{project.project_id}/runs/{run.run.run_id}/plan.json"
    RenderPlan.create(
        video_material_id=video,
        video_expected_fingerprint=MaterialFingerprint("a" * 64),
        music_material_id=music,
        music_expected_fingerprint=MaterialFingerprint("b" * 64),
        fps=30,
        clips=[{
            "timestamp": "00:00:00,000-00:00:01,000",
            "output_frame_range": [0, 30],
        }],
        planners_metadata={"test": True},
    ).write(managed_configuration.data_root / relative_plan)
    completed_run = runs.complete(
        CompleteRunCommand(
            command_id(),
            run.run.run_id,
            run.attempt.attempt_id,
            relative_plan,
        )
    )
    assert completed_run.run.status is RunStatus.COMPLETE

    render = renders.create(
        CreateRenderVariantCommand(
            command_id(),
            completed_run.frozen_edit.edit_id,
            "dialogue",
        )
    )
    duplicate = renders.create(
        CreateRenderVariantCommand(
            command_id(),
            completed_run.frozen_edit.edit_id,
            "dialogue",
        )
    )
    assert not duplicate.created
    assert (
        duplicate.render_variant.render_variant_id
        == render.render_variant.render_variant_id
    )
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
            assert (
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            )


def test_project_delete_cleans_only_owned_artifacts_and_replays_cleanup(
    managed_configuration: EffectiveConfiguration,
) -> None:
    store = SQLiteApplicationStore(managed_configuration.data_root)
    projects, project, video, music = create_ready_project(
        managed_configuration,
        store,
    )
    other = projects.create(CreateProjectCommand(command_id(), "Other Project"))
    runs = RunsService(managed_configuration, store)
    jobs = JobsService(managed_configuration, store)
    run = runs.create(CreateRunCommand(command_id(), project.project_id))
    jobs.stop(StopAttemptCommand(command_id(), run.attempt.attempt_id))

    owned = managed_configuration.data_root / "projects" / str(project.project_id)
    owned.mkdir(parents=True)
    (owned / "artifact.json").write_text("owned", encoding="utf-8")
    retained = managed_configuration.data_root / "projects" / str(other.project_id)
    retained.mkdir(parents=True)
    (retained / "artifact.json").write_text("other", encoding="utf-8")
    logs = managed_configuration.data_root / "logs" / "jobs"
    logs.mkdir(parents=True)
    owned_log = logs / f"{run.job.job_id}.log"
    owned_log.write_text("owned", encoding="utf-8")
    retained_log = logs / "unrelated.log"
    retained_log.write_text("other", encoding="utf-8")
    delete_command = DeleteProjectCommand(command_id(), project.project_id)

    first = projects.delete(delete_command)
    replay = projects.delete(delete_command)

    assert first == replay
    assert not owned.exists()
    assert not owned_log.exists()
    assert (retained / "artifact.json").read_text(encoding="utf-8") == "other"
    assert retained_log.read_text(encoding="utf-8") == "other"
    assert projects.get(other.project_id).name == "Other Project"
    materials = MaterialsService(managed_configuration)
    assert materials.get(video) is not None
    assert materials.get(music) is not None


def test_project_delete_unlinks_owner_symlink_without_following_it(
    managed_configuration: EffectiveConfiguration,
) -> None:
    store = SQLiteApplicationStore(managed_configuration.data_root)
    projects = ProjectsService(managed_configuration, store)
    project = projects.create(CreateProjectCommand(command_id(), "Symlink Project"))
    outside = managed_configuration.data_root.parent / f"outside-{uuid4()}"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep", encoding="utf-8")
    owner = managed_configuration.data_root / "projects" / str(project.project_id)
    owner.parent.mkdir(parents=True, exist_ok=True)
    owner.symlink_to(outside, target_is_directory=True)

    projects.delete(DeleteProjectCommand(command_id(), project.project_id))

    assert not owner.exists()
    assert not owner.is_symlink()
    assert marker.read_text(encoding="utf-8") == "keep"
