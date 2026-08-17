from __future__ import annotations

import ast
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from cutmaster.application.workflow.contracts import ExecuteManagedWorkflowCommand
from cutmaster.application.workflow.coordinator import ManagedWorkflowCoordinator
from cutmaster.domain.attempts import AttemptStatus
from cutmaster.domain.ids import (
    AttemptId,
    FrozenEditId,
    JobId,
    MaterialId,
    ProjectId,
    RenderVariantId,
    RunId,
)
from cutmaster.domain.materials import (
    Material,
    MaterialCondition,
    MaterialFingerprint,
    MaterialType,
)


class _Materials:
    def __init__(self, root: Path) -> None:
        self._root = root
        self._materials: dict[MaterialId, Material] = {}
        self._bindings: dict[MaterialId, SimpleNamespace] = {}
        self.subtitle: Path | None = None

    def ensure(
        self,
        source: Path,
        material_type: MaterialType,
        name: str | None,
    ) -> Material:
        material_id = MaterialId.new()
        material = Material(
            material_id,
            material_type,
            name or source.stem,
            MaterialFingerprint("a" * 64),
            MaterialCondition.QUEUED,
        )
        memory_root = self._root / "media" / material_type.value / str(material_id)
        memory_root.mkdir(parents=True)
        self._materials[material_id] = material
        self._bindings[material_id] = SimpleNamespace(
            material=material,
            memory_root=memory_root,
        )
        return material

    def find_by_name(self, material_type: MaterialType, name: str):
        return next(
            (
                material
                for material in self._materials.values()
                if material.material_type is material_type and material.name == name
            ),
            None,
        )

    def get(self, material_id: MaterialId):
        return self._materials.get(material_id)

    def mark_ready(self, material_id: MaterialId) -> Material:
        material = self._materials[material_id]
        ready = Material(
            material.material_id,
            material.material_type,
            material.name,
            material.fingerprint,
            MaterialCondition.READY,
        )
        self._materials[material_id] = ready
        self._bindings[material_id].material = ready
        filename = (
            "analysis_result.json"
            if ready.material_type is MaterialType.VIDEO
            else "music_analysis_result.json"
        )
        (self._bindings[material_id].memory_root / filename).write_text(
            "{}",
            encoding="utf-8",
        )
        if ready.material_type is MaterialType.VIDEO:
            (self._bindings[material_id].memory_root / "dialogues.json").write_text(
                "[]",
                encoding="utf-8",
            )
        else:
            (self._bindings[material_id].memory_root / "music_memory.json").write_text(
                "{}",
                encoding="utf-8",
            )
        return ready

    def lease(self, material_id: MaterialId):
        return nullcontext(self._bindings[material_id])

    def read_lease(self, material_id: MaterialId):
        return nullcontext(self._bindings[material_id])

    def ensure_subtitle(self, _binding, subtitle: Path) -> None:
        self.subtitle = subtitle


class _Jobs:
    def __init__(self) -> None:
        self.attempts: dict[AttemptId, SimpleNamespace] = {}
        self.material_by_job: dict[JobId, MaterialId] = {}

    def submission(self):
        attempt_id = AttemptId.new()
        job_id = JobId.new()
        attempt = SimpleNamespace(
            attempt_id=attempt_id,
            status=AttemptStatus.QUEUED,
            error_message=None,
        )
        self.attempts[attempt_id] = attempt
        return SimpleNamespace(
            attempt=attempt,
            job=SimpleNamespace(job_id=job_id),
        )

    def enqueue_material_analysis(self, command):
        submission = self.submission()
        self.material_by_job[submission.job.job_id] = command.material_id
        return submission

    def get_attempt(self, attempt_id: AttemptId):
        return self.attempts[attempt_id]

    def complete(self, attempt_id: AttemptId) -> None:
        self.attempts[attempt_id].status = AttemptStatus.COMPLETE


class _Projects:
    def __init__(self) -> None:
        self.created = []
        self.setup = []

    def create(self, command):
        self.created.append(command)
        return SimpleNamespace(project_id=ProjectId.new())

    def save_setup(self, command):
        self.setup.append(command)
        return SimpleNamespace(project_id=command.project_id)


class _Runs:
    def __init__(self, root: Path, jobs: _Jobs) -> None:
        self._root = root
        self._jobs = jobs
        self.created = []
        self.run_by_job: dict[JobId, SimpleNamespace] = {}
        self.edits: dict[RunId, list[SimpleNamespace]] = {}

    def create(self, command):
        self.created.append(command)
        submission = self._jobs.submission()
        run = SimpleNamespace(run_id=RunId.new(), project_id=command.project_id)
        self.run_by_job[submission.job.job_id] = run
        return SimpleNamespace(
            run=run,
            job=submission.job,
            attempt=submission.attempt,
        )

    def complete_job(self, job_id: JobId) -> None:
        run = self.run_by_job[job_id]
        attempt = next(
            attempt
            for attempt in self._jobs.attempts.values()
            if attempt.status is AttemptStatus.QUEUED
        )
        edit_id = FrozenEditId.new()
        directory = (
            self._root
            / "projects"
            / str(run.project_id)
            / "runs"
            / str(run.run_id)
            / "frozen_edits"
            / str(edit_id)
        )
        directory.mkdir(parents=True)
        plan = directory / "render_plan.json"
        plan.write_text("{}", encoding="utf-8")
        diagnostics = directory / "selection_diagnostics.json"
        diagnostics.write_text("{}", encoding="utf-8")
        (directory / "review_bundle.json").write_text(
            json.dumps(
                {
                    "artifacts": {
                        "selection_diagnostics": {
                            "path": diagnostics.name,
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        self.edits[run.run_id] = [
            SimpleNamespace(
                edit_id=edit_id,
                plan=SimpleNamespace(
                    relative_path=plan.relative_to(self._root).as_posix()
                ),
            )
        ]
        self._jobs.complete(attempt.attempt_id)

    def get(self, run_id: RunId):
        return next(run for run in self.run_by_job.values() if run.run_id == run_id)

    def list_frozen_edits(self, run_id: RunId):
        return self.edits[run_id]

    def usage(self, _run_id: RunId):
        return SimpleNamespace(
            run_total={"total_tokens": 321, "total_cost_yuan": 0.12}
        )


class _Renders:
    def __init__(self, root: Path, jobs: _Jobs) -> None:
        self._root = root
        self._jobs = jobs
        self.created = []
        self.variant_by_job: dict[JobId, SimpleNamespace] = {}

    def create(self, command):
        self.created.append(command)
        submission = self._jobs.submission()
        variant = SimpleNamespace(
            render_variant_id=RenderVariantId.new(),
            master=None,
            duration_sec=None,
        )
        self.variant_by_job[submission.job.job_id] = variant
        return SimpleNamespace(
            render_variant=variant,
            job=submission.job,
            attempt=submission.attempt,
        )

    def complete_job(self, job_id: JobId) -> None:
        variant = self.variant_by_job[job_id]
        attempt = next(
            attempt
            for attempt in self._jobs.attempts.values()
            if attempt.status is AttemptStatus.QUEUED
        )
        output = self._root / "renders" / str(variant.render_variant_id) / "output.mp4"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"video")
        variant.master = SimpleNamespace(
            relative_path=output.relative_to(self._root).as_posix()
        )
        variant.duration_sec = 59.5
        self._jobs.complete(attempt.attempt_id)

    def get(self, render_variant_id: RenderVariantId):
        return next(
            variant
            for variant in self.variant_by_job.values()
            if variant.render_variant_id == render_variant_id
        )


class _Executor:
    def __init__(self, application) -> None:
        self.application = application
        self.calls: list[tuple[str, JobId, str]] = []

    def execute_material_job(self, job_id: JobId, **kwargs) -> None:
        self.calls.append(("material", job_id, kwargs["worker_id"]))
        material_id = self.application.jobs.material_by_job[job_id]
        self.application.materials.mark_ready(material_id)
        attempt = next(
            attempt
            for attempt in self.application.jobs.attempts.values()
            if attempt.status is AttemptStatus.QUEUED
        )
        self.application.jobs.complete(attempt.attempt_id)

    def execute_run_job(self, job_id: JobId, **kwargs) -> None:
        self.calls.append(("run", job_id, kwargs["worker_id"]))
        self.application.runs.complete_job(job_id)

    def execute_render_job(self, job_id: JobId, **kwargs) -> None:
        self.calls.append(("render", job_id, kwargs["worker_id"]))
        self.application.renders.complete_job(job_id)


def _application(root: Path):
    jobs = _Jobs()
    application = SimpleNamespace(
        materials=_Materials(root),
        projects=_Projects(),
        jobs=jobs,
        settings=SimpleNamespace(
            effective_configuration=SimpleNamespace(data_root=root)
        ),
    )
    application.runs = _Runs(root, jobs)
    application.renders = _Renders(root, jobs)
    return application


def test_coordinator_never_depends_on_an_adapter() -> None:
    source_path = (
        Path(__file__).parents[3]
        / "src/cutmaster/application/workflow/coordinator.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    )
    assert not any(name.startswith("cutmaster.adapters") for name in imports)


def test_execute_and_wait_preserves_managed_history_and_portable_artifacts(
    tmp_path: Path,
) -> None:
    root = tmp_path / ".cutmaster"
    root.mkdir()
    application = _application(root)
    executor = _Executor(application)
    coordinator = ManagedWorkflowCoordinator(application, job_executor=executor)
    video = tmp_path / "movie.mp4"
    music = tmp_path / "score.mp3"
    subtitle = tmp_path / "dialogue.srt"
    video.write_bytes(b"video")
    music.write_bytes(b"music")
    subtitle.write_text("subtitle", encoding="utf-8")

    result = coordinator.execute_and_wait(
        ExecuteManagedWorkflowCommand(
            prompt="Create a coherent character arc",
            video_path=video,
            audio_path=music,
            subtitle_path=subtitle,
            video_material_name="Feature",
            music_material_name="Score",
            project_name="Case Study",
            target_output_length_sec=60,
            target_shot_length_sec=3.5,
            prompt_type="event",
            video_title="Feature Film",
            max_clip_duration_sec=5,
            audio_mode="dialogue",
        )
    )

    assert result.status == "success"
    assert result.video_material_name == "Feature"
    assert result.music_material_name == "Score"
    assert result.actual_output_length_sec == 59.5
    assert result.dialogue_audio_included is True
    assert result.model_usage == {"total_tokens": 321, "total_cost_yuan": 0.12}
    assert application.materials.subtitle == subtitle.resolve()
    assert [call[0] for call in executor.calls] == [
        "material",
        "material",
        "run",
        "render",
    ]
    assert all(call[2].startswith("local-managed-") for call in executor.calls)
    assert len(application.projects.created) == 1
    assert len(application.projects.setup) == 1
    assert len(application.runs.created) == 1
    assert len(application.renders.created) == 1

    assert result.artifacts["planners.render_plan"].endswith("render_plan.json")
    assert result.artifacts["renderer.output_video"].endswith("output.mp4")
    assert result.artifacts["analyser.video_result"].endswith("analysis_result.json")
    assert result.artifacts["analyser.music_memory"].endswith("music_memory.json")
    assert result.artifacts["planners.selection_diagnostics"].endswith(
        "selection_diagnostics.json"
    )
    result_path = root.joinpath(*result.artifacts["workflow.result"].split("/"))
    usage_path = root.joinpath(*result.artifacts["workflow.model_usage"].split("/"))
    assert json.loads(result_path.read_text(encoding="utf-8")) == result.to_dict()
    assert json.loads(usage_path.read_text(encoding="utf-8")) == result.model_usage
    assert all(not Path(path).is_absolute() for path in result.artifacts.values())


def test_component_methods_return_managed_receipts(tmp_path: Path) -> None:
    root = tmp_path / ".cutmaster"
    root.mkdir()
    application = _application(root)
    executor = _Executor(application)
    coordinator = ManagedWorkflowCoordinator(application, job_executor=executor)
    video_source = tmp_path / "movie.mp4"
    music_source = tmp_path / "score.mp3"
    video_source.write_bytes(b"video")
    music_source.write_bytes(b"music")

    video = coordinator.analyse_video(video_source, material_name="Video")
    music = coordinator.analyse_music(music_source, material_name="Music")
    plan = coordinator.plan(
        video_material="Video",
        music_material="Music",
        prompt="A concise arc",
        project_name="Components",
        target_output_length_sec=30,
    )
    render = coordinator.render(FrozenEditId.parse(plan["frozen_edit_id"]))

    assert video["condition"] == "ready"
    assert music["condition"] == "ready"
    assert plan["render_plan"].endswith("render_plan.json")
    assert render["output_video"].endswith("output.mp4")


def test_failed_job_is_reported_with_operation_context(tmp_path: Path) -> None:
    application = _application(tmp_path)
    coordinator = ManagedWorkflowCoordinator(
        application,
        job_executor=SimpleNamespace(),
    )
    attempt = SimpleNamespace(
        status=AttemptStatus.FAILED,
        error_message="provider unavailable",
    )
    application.jobs.attempts[attempt_id := AttemptId.new()] = attempt

    with pytest.raises(
        RuntimeError,
        match="ASTER Planners failed: provider unavailable",
    ):
        coordinator._require_complete(attempt_id, "ASTER Planners")
