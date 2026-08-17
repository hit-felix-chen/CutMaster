"""Shared local adapter orchestration for Web-visible managed executions."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from cutmaster.adapters.web.material_worker import execute_material_job
from cutmaster.adapters.web.render_worker import execute_render_job
from cutmaster.adapters.web.run_worker import execute_run_job
from cutmaster.application import CutMasterApplication
from cutmaster.application.jobs import EnqueueMaterialAnalysisCommand
from cutmaster.application.projects import (
    CreateProjectCommand,
    SaveProjectSetupCommand,
)
from cutmaster.application.renders import CreateRenderVariantCommand
from cutmaster.application.runs import CreateRunCommand
from cutmaster.contracts.managed_workflow import (
    ExecuteManagedWorkflowCommand,
    ManagedWorkflowResult,
)
from cutmaster.domain.attempts import AttemptStatus, TERMINAL_ATTEMPT_STATUSES
from cutmaster.domain.ids import FrozenEditId
from cutmaster.domain.materials import MaterialCondition, MaterialType


class LocalManagedWorkflow:
    """Run the same durable managed lifecycle used by the local Web workspace."""

    def __init__(self, application: CutMasterApplication) -> None:
        if not isinstance(application, CutMasterApplication):
            raise TypeError("application must be a CutMasterApplication")
        self._application = application

    def analyse_video(
        self,
        source_path: Path,
        *,
        material_name: str = "",
        subtitle_path: Path | None = None,
    ) -> dict[str, Any]:
        material = self._ensure_ready_material(
            MaterialType.VIDEO,
            source_path=source_path,
            candidate_name=material_name,
            subtitle_path=subtitle_path,
        )
        return self._material_receipt(material)

    def analyse_music(
        self,
        source_path: Path,
        *,
        material_name: str = "",
    ) -> dict[str, Any]:
        material = self._ensure_ready_material(
            MaterialType.MUSIC,
            source_path=source_path,
            candidate_name=material_name,
        )
        return self._material_receipt(material)

    def plan(
        self,
        *,
        video_material: str,
        music_material: str,
        prompt: str,
        project_name: str,
        target_output_length_sec: float,
        target_shot_length_sec: float = 4.0,
        prompt_type: str = "event",
        video_title: str = "",
        max_clip_duration_sec: float | None = None,
    ) -> dict[str, Any]:
        video = self._ensure_ready_material(
            MaterialType.VIDEO,
            existing_name=video_material,
        )
        music = self._ensure_ready_material(
            MaterialType.MUSIC,
            existing_name=music_material,
        )
        project, run, edit = self._plan(
            video,
            music,
            prompt=prompt,
            project_name=project_name,
            target_output_length_sec=target_output_length_sec,
            target_shot_length_sec=target_shot_length_sec,
            prompt_type=prompt_type,
            video_title=video_title,
            max_clip_duration_sec=max_clip_duration_sec,
        )
        return {
            "status": "success",
            "project_id": str(project.project_id),
            "run_id": str(run.run_id),
            "frozen_edit_id": str(edit.edit_id),
            "render_plan": edit.plan.relative_path,
            "artifact_root": str(self._data_root),
            "schema_version": "2.0",
        }

    def render(
        self,
        edit_id: FrozenEditId,
        *,
        audio_mode: str = "dialogue",
    ) -> dict[str, Any]:
        variant = self._render(edit_id, audio_mode=audio_mode)
        if variant.master is None or variant.duration_sec is None:
            raise RuntimeError("Managed Renderer completed without a master")
        return {
            "status": "success",
            "frozen_edit_id": str(edit_id),
            "render_variant_id": str(variant.render_variant_id),
            "output_video": variant.master.relative_path,
            "actual_output_length_sec": variant.duration_sec,
            "artifact_root": str(self._data_root),
            "schema_version": "2.0",
        }

    def execute_workflow(
        self,
        command: ExecuteManagedWorkflowCommand,
    ) -> ManagedWorkflowResult:
        if not isinstance(command, ExecuteManagedWorkflowCommand):
            raise TypeError("command must be an ExecuteManagedWorkflowCommand")
        video = self._resolve_material(
            MaterialType.VIDEO,
            source_path=command.video_path,
            existing_name=command.video_material,
            candidate_name=command.video_material_name,
            subtitle_path=command.subtitle_path,
        )
        music = self._resolve_material(
            MaterialType.MUSIC,
            source_path=command.audio_path,
            existing_name=command.music_material,
            candidate_name=command.music_material_name,
        )
        project, run, edit = self._plan(
            video,
            music,
            prompt=command.prompt,
            project_name=command.project_name,
            target_output_length_sec=command.target_output_length_sec,
            target_shot_length_sec=command.target_shot_length_sec,
            prompt_type=command.prompt_type,
            video_title=command.video_title,
            max_clip_duration_sec=command.max_clip_duration_sec,
        )
        variant = self._render(edit.edit_id, audio_mode=command.audio_mode)
        if variant.master is None or variant.duration_sec is None:
            raise RuntimeError("Managed Renderer completed without a master")
        usage = dict(self._application.runs.usage(run.run_id).run_total)
        artifacts = self._artifact_manifest(
            project_id=str(project.project_id),
            run_id=str(run.run_id),
            video_material_id=video.material_id,
            music_material_id=music.material_id,
            plan_relative_path=edit.plan.relative_path,
            master_relative_path=variant.master.relative_path,
            usage=usage,
        )
        result = ManagedWorkflowResult(
            status="success",
            project_id=str(project.project_id),
            run_id=str(run.run_id),
            frozen_edit_id=str(edit.edit_id),
            render_variant_id=str(variant.render_variant_id),
            video_material_id=str(video.material_id),
            music_material_id=str(music.material_id),
            video_material_name=video.name,
            music_material_name=music.name,
            target_output_length_sec=command.target_output_length_sec,
            actual_output_length_sec=variant.duration_sec,
            dialogue_audio_included=command.audio_mode == "dialogue",
            artifact_root=str(self._data_root),
            artifacts=artifacts,
            model_usage=usage,
        )
        self._write_result(result)
        return result

    @property
    def _data_root(self) -> Path:
        return self._application.settings.effective_configuration.data_root.resolve()

    def _resolve_material(
        self,
        material_type: MaterialType,
        *,
        source_path: Path | None,
        existing_name: str,
        candidate_name: str,
        subtitle_path: Path | None = None,
    ):
        if source_path is not None:
            return self._ensure_ready_material(
                material_type,
                source_path=source_path,
                candidate_name=candidate_name,
                subtitle_path=subtitle_path,
            )
        return self._ensure_ready_material(
            material_type,
            existing_name=existing_name,
        )

    def _ensure_ready_material(
        self,
        material_type: MaterialType,
        *,
        source_path: Path | None = None,
        existing_name: str = "",
        candidate_name: str = "",
        subtitle_path: Path | None = None,
    ):
        if source_path is not None:
            material = self._application.materials.ensure(
                source_path.resolve(),
                material_type,
                candidate_name or None,
            )
        else:
            material = self._application.materials.find_by_name(
                material_type,
                existing_name,
            )
            if material is None:
                raise FileNotFoundError(
                    f"Unknown {material_type.value} Material: {existing_name!r}"
                )
        if subtitle_path is not None:
            with self._application.materials.lease(material.material_id) as binding:
                self._application.materials.ensure_subtitle(
                    binding,
                    subtitle_path.resolve(),
                )
        if material.condition is MaterialCondition.READY:
            return material
        submission = self._application.jobs.enqueue_material_analysis(
            EnqueueMaterialAnalysisCommand(str(uuid4()), material.material_id)
        )
        execute_material_job(
            self._application,
            submission.job.job_id,
            worker_id=f"local-managed-{os.getpid()}",
        )
        self._require_complete(submission.attempt.attempt_id, "Material Analysis")
        ready = self._application.materials.get(material.material_id)
        if ready is None or ready.condition is not MaterialCondition.READY:
            raise RuntimeError("Material Analysis completed without Ready Material")
        return ready

    def _plan(
        self,
        video,
        music,
        *,
        prompt: str,
        project_name: str,
        target_output_length_sec: float,
        target_shot_length_sec: float,
        prompt_type: str,
        video_title: str,
        max_clip_duration_sec: float | None,
    ):
        project = self._application.projects.create(
            CreateProjectCommand(str(uuid4()), project_name)
        )
        project = self._application.projects.save_setup(
            SaveProjectSetupCommand(
                str(uuid4()),
                project.project_id,
                (video.material_id,),
                (music.material_id,),
                prompt,
                target_output_length_sec,
            )
        )
        submission = self._application.runs.create(
            CreateRunCommand(
                str(uuid4()),
                project.project_id,
                target_shot_length_sec=target_shot_length_sec,
                prompt_type=prompt_type,
                video_title=video_title,
                max_clip_duration_sec=max_clip_duration_sec,
            )
        )
        execute_run_job(
            self._application,
            submission.job.job_id,
            worker_id=f"local-managed-{os.getpid()}",
        )
        self._require_complete(submission.attempt.attempt_id, "ASTER Planners")
        run = self._application.runs.get(submission.run.run_id)
        edits = self._application.runs.list_frozen_edits(run.run_id)
        if len(edits) != 1:
            raise RuntimeError("Completed initial ASTER Run must own one Frozen Edit")
        return project, run, edits[0]

    def _render(self, edit_id: FrozenEditId, *, audio_mode: str):
        submission = self._application.renders.create(
            CreateRenderVariantCommand(str(uuid4()), edit_id, audio_mode)
        )
        if submission.job is None or submission.attempt is None:
            raise RuntimeError("Managed Render submission has no Job")
        execute_render_job(
            self._application,
            submission.job.job_id,
            worker_id=f"local-managed-{os.getpid()}",
        )
        self._require_complete(submission.attempt.attempt_id, "Renderer")
        return self._application.renders.get(
            submission.render_variant.render_variant_id
        )

    def _require_complete(self, attempt_id, operation: str) -> None:
        attempt = self._application.jobs.get_attempt(attempt_id)
        while attempt.status not in TERMINAL_ATTEMPT_STATUSES:
            time.sleep(0.1)
            attempt = self._application.jobs.get_attempt(attempt_id)
        if attempt.status is not AttemptStatus.COMPLETE:
            detail = attempt.error_message or attempt.status.value
            raise RuntimeError(f"{operation} failed: {detail}")

    def _artifact_manifest(
        self,
        *,
        project_id: str,
        run_id: str,
        video_material_id,
        music_material_id,
        plan_relative_path: str,
        master_relative_path: str,
        usage: dict[str, Any],
    ) -> dict[str, str]:
        artifacts: dict[str, str] = {
            "planners.render_plan": plan_relative_path,
            "renderer.output_video": master_relative_path,
        }
        for material_id, names in (
            (
                video_material_id,
                {
                    "analyser.video_result": "analysis_result.json",
                    "analyser.dialogues": "dialogues.json",
                    "analyser.dialogue_subtitle": "dialogue_merged.srt",
                },
            ),
            (
                music_material_id,
                {
                    "analyser.music_result": "music_analysis_result.json",
                    "analyser.music_memory": "music_memory.json",
                },
            ),
        ):
            with self._application.materials.read_lease(material_id) as binding:
                for logical_key, filename in names.items():
                    candidate = binding.memory_root / filename
                    if candidate.is_file() and not candidate.is_symlink():
                        artifacts[logical_key] = self._relative(candidate)
        plan = self._data_root.joinpath(*plan_relative_path.split("/"))
        review_manifest = plan.parent / "review_bundle.json"
        if review_manifest.is_file() and not review_manifest.is_symlink():
            artifacts["planners.review_bundle"] = self._relative(review_manifest)
            payload = json.loads(review_manifest.read_text(encoding="utf-8"))
            entries = payload.get("artifacts")
            if isinstance(entries, dict):
                for name, entry in entries.items():
                    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                        continue
                    candidate = review_manifest.parent / entry["path"]
                    if candidate.is_file() and not candidate.is_symlink():
                        artifacts[f"planners.{name}"] = self._relative(candidate)
        run_directory = self._data_root / "projects" / project_id / "runs" / run_id
        usage_path = run_directory / "model_usage.json"
        self._write_json_atomic(usage_path, usage)
        artifacts["workflow.model_usage"] = self._relative(usage_path)
        result_path = run_directory / "result.json"
        artifacts["workflow.result"] = self._relative(result_path)
        return artifacts

    def _write_result(self, result: ManagedWorkflowResult) -> None:
        path = self._data_root.joinpath(*result.artifacts["workflow.result"].split("/"))
        self._write_json_atomic(path, result.to_dict())

    def _relative(self, path: Path) -> str:
        resolved = path.resolve(strict=True)
        return resolved.relative_to(self._data_root).as_posix()

    @staticmethod
    def _write_json_atomic(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _material_receipt(material) -> dict[str, Any]:
        return {
            "status": "success",
            "material_id": str(material.material_id),
            "material_type": material.material_type.value,
            "material_name": material.name,
            "condition": material.condition.value,
            "schema_version": "2.0",
        }


__all__ = ["LocalManagedWorkflow"]
