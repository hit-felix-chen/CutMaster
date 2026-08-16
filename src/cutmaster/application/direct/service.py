"""Synchronous component and complete-workflow Application use cases."""

from __future__ import annotations

import json
import shutil
import time
from contextlib import ExitStack
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from cutmaster.application.direct.artifacts import (
    ARTIFACT_MANIFEST_VERSION,
    DirectArtifactLayout,
    build_artifact_manifest,
    publish_result,
)
from cutmaster.application.direct.commands import (
    AnalyseMusicCommand,
    AnalyseVideoCommand,
    PlanCommand,
    RenderCommand,
)
from cutmaster.application.materials.service import MaterialsService, MaterialView
from cutmaster.application.ports.material_catalog import MaterialBinding
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.configuration.runtime import resolve_runtime_config
from cutmaster.contracts.workflow import ExecuteWorkflowCommand, WorkflowResult
from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialCondition, MaterialType
from cutmaster.infrastructure.observability.logging import configure_logging, log_event
from cutmaster.workflow.ports import ProgressReporter
from cutmaster.workflow.contracts import (
    AnalyseMusicRequest,
    AnalyseVideoRequest,
    AnalysisWorkspace,
    AnalysedMusicRuntimeHandle,
    AnalysedVideoRuntimeHandle,
    MaterialRuntimeHandle,
    MusicAnalysisOptions,
    MusicAnalysisResult,
    PlannersRequest,
    PlannersBrief,
    PlannersOptions,
    PlannersWorkspace,
    RenderOptions,
    RenderOutputTarget,
    RenderPlan,
    RenderRequest,
    RenderRuntimeBindings,
    VideoAnalysisOptions,
    VideoAnalysisResult,
)


@dataclass(frozen=True)
class _ComponentInvocationPaths:
    """Workspace and invocation-level log allocated for one component call."""

    workspace: Path
    log_path: Path


class DirectService:
    """Execute CutMaster synchronously without HTTP or managed product history."""

    __slots__ = ("_effective_configuration", "_materials")

    def __init__(
        self,
        effective_configuration: EffectiveConfiguration,
        materials: MaterialsService,
    ) -> None:
        if not isinstance(effective_configuration, EffectiveConfiguration):
            raise TypeError("effective_configuration must be EffectiveConfiguration")
        if not isinstance(materials, MaterialsService):
            raise TypeError("materials must be MaterialsService")
        self._effective_configuration = effective_configuration
        self._materials = materials

    def analyse_video(self, command: AnalyseVideoCommand) -> VideoAnalysisResult:
        paths = self._component_paths(command.output_dir, "analyser")
        workspace = paths.workspace
        configure_logging(paths.log_path, console_color=True)
        view = self._materials.ensure(
            command.video_path,
            MaterialType.VIDEO,
            command.material_name or None,
        )
        with self._materials.lease(view.material_id) as binding:
            return self._analyse_video(
                binding,
                workspace,
                video_title=command.video_title,
                subtitle_path=command.subtitle_path,
                material_reused=view.reused,
            )

    # The CLI spelling is retained as an ergonomic alias.
    analyse = analyse_video

    def analyse_music(self, command: AnalyseMusicCommand) -> MusicAnalysisResult:
        paths = self._component_paths(command.output_dir, "analyser/music")
        workspace = paths.workspace
        configure_logging(paths.log_path, console_color=True)
        view = self._materials.ensure(
            command.audio_path,
            MaterialType.MUSIC,
            command.material_name or None,
        )
        with self._materials.lease(view.material_id) as binding:
            return self._analyse_music(
                binding,
                workspace,
                material_reused=view.reused,
            )

    def plan(self, command: PlanCommand):
        paths = self._component_paths(command.output_dir, "planners")
        workspace = paths.workspace
        configure_logging(paths.log_path, console_color=True)
        with ExitStack() as stack:
            video_view, video_binding, video_analysis = self._resolve_video_for_plan(
                command,
                stack,
            )
            music_view, music_binding, music_analysis = self._resolve_music_for_plan(
                command,
                stack,
                workspace,
            )
            del video_view, music_view, video_binding, music_binding
            return self._plan(
                video_analysis,
                music_analysis,
                workspace,
                prompt=command.prompt,
                target_output_length_sec=command.target_output_length_sec,
                target_shot_length_sec=command.target_shot_length_sec,
                prompt_type=command.prompt_type,
                video_title=video_analysis.video.material.material_name,
                max_clip_duration_sec=command.max_clip_duration_sec,
                overwrite=command.overwrite,
                progress_reporter=command.progress_reporter,
            )

    def render(self, command: RenderCommand):
        paths = self._component_paths(command.output_dir, "renderer")
        workspace = paths.workspace
        configure_logging(paths.log_path, console_color=True)
        plan = RenderPlan.read(command.plan_path.resolve())
        with ExitStack() as stack:
            bindings = self._lease_render_bindings(plan, stack)
            return self._render(
                plan,
                bindings,
                workspace,
                audio_mode=command.audio_mode,
                overwrite=command.overwrite,
            )

    def execute_workflow(
        self,
        command: ExecuteWorkflowCommand,
    ) -> WorkflowResult:
        if not isinstance(command, ExecuteWorkflowCommand):
            raise TypeError("command must be an ExecuteWorkflowCommand")
        layout = DirectArtifactLayout.allocate(
            self._effective_configuration.data_root,
            command.output_dir,
        )
        if layout.output_video.exists() and not command.overwrite:
            raise FileExistsError(
                "Rendered output already exists; pass --overwrite: "
                f"{layout.output_video}"
            )
        layout.workflow_result.unlink(missing_ok=True)
        configure_logging(layout.workflow_log, console_color=True)
        started = time.monotonic()
        log_event(
            "INFO",
            "cutmaster",
            "workflow.start",
            "CutMaster workflow started",
            audio_mode=command.audio_mode,
            target_duration_sec=command.target_output_length_sec,
            output_dir=layout.root,
        )

        video_view = self._resolve_or_ensure(
            MaterialType.VIDEO,
            source_path=command.video_path,
            existing_name=command.video_material,
            candidate_name=command.video_material_name,
        )
        music_view = self._resolve_or_ensure(
            MaterialType.MUSIC,
            source_path=command.audio_path,
            existing_name=command.music_material,
            candidate_name=command.music_material_name,
        )
        with ExitStack() as stack:
            leased = self._lease_many(
                (video_view.material_id, music_view.material_id),
                stack,
            )
            video_binding = leased[video_view.material_id]
            music_binding = leased[music_view.material_id]
            # A raw-path invocation can resolve to an already analysed Material.
            # Validate any supplied subtitle against that Material before taking
            # the READY fast path; otherwise a different subtitle could be
            # silently ignored while the cached Video Memory is reused.
            if command.subtitle_path is not None:
                self._materials.ensure_subtitle(
                    video_binding,
                    command.subtitle_path,
                )
            if video_binding.material.condition is MaterialCondition.READY:
                analysis = self._load_video_analysis(
                    video_binding,
                    layout.analyser_dir,
                    material_reused=True,
                )
            else:
                analysis = self._analyse_video(
                    video_binding,
                    layout.analyser_dir,
                    video_title=command.video_title,
                    subtitle_path=command.subtitle_path,
                    material_reused=video_view.reused,
                )
            if music_binding.material.condition is MaterialCondition.READY:
                music_analysis = self._load_music_analysis(
                    music_binding,
                    layout.music_analyser_dir,
                    material_reused=True,
                )
            else:
                music_analysis = self._analyse_music(
                    music_binding,
                    layout.music_analyser_dir,
                    material_reused=music_view.reused,
                )
            planners_result = self._plan(
                analysis,
                music_analysis,
                layout.planners_dir,
                prompt=command.prompt,
                target_output_length_sec=command.target_output_length_sec,
                target_shot_length_sec=command.target_shot_length_sec,
                prompt_type=command.prompt_type,
                video_title=command.video_title,
                max_clip_duration_sec=command.max_clip_duration_sec,
                overwrite=command.overwrite,
                progress_reporter=None,
            )
            rendered = self._render(
                planners_result.render_plan,
                RenderRuntimeBindings(
                    video=self._runtime_handle(video_binding),
                    music=self._runtime_handle(music_binding),
                ),
                layout.renderer_dir,
                audio_mode=command.audio_mode,
                overwrite=command.overwrite,
            )

            timings = {
                "analyser": analysis.elapsed_sec + music_analysis.elapsed_sec,
                "analyser.video": analysis.elapsed_sec,
                "analyser.music": music_analysis.elapsed_sec,
                "planners": planners_result.wall_clock_sec,
                "renderer": rendered.wall_clock_sec,
                **{
                    f"planners.{name}": value
                    for name, value in planners_result.stage_timings_sec.items()
                },
                **{
                    f"renderer.{name}": value
                    for name, value in rendered.stage_timings_sec.items()
                },
            }
            from cutmaster.infrastructure.models.openai_compatible import (
                merge_usage_summaries,
            )

            current_usage = merge_usage_summaries(
                [analysis.model_usage_summary, planners_result.model_usage_summary]
            )
            cumulative_usage = merge_usage_summaries(
                [
                    analysis.model_usage_cumulative_summary,
                    planners_result.model_usage_cumulative_summary,
                ]
            )
            model_usage = self._write_workflow_usage(
                layout,
                analysis,
                planners_result,
                current_usage,
                cumulative_usage,
            )
            # Logging is a required artifact and must be flushed/visible before
            # the final success result is published.
            log_event(
                "SUCCESS",
                "cutmaster",
                "workflow.complete",
                "CutMaster workflow completed",
                output_path=rendered.output_video,
                wall_clock_sec=time.monotonic() - started,
                output_duration_sec=rendered.duration_sec,
                clips=planners_result.num_planned_clips,
            )
            artifacts = build_artifact_manifest(layout)
            result = WorkflowResult(
                status="success",
                analysis_result=str(layout.analysis_result),
                planners_result=str(layout.planners_result),
                render_result=str(layout.render_result),
                render_plan=str(layout.render_plan),
                output_video=str(rendered.output_video),
                material_directory=str(video_binding.memory_root),
                target_output_length_sec=command.target_output_length_sec,
                actual_output_length_sec=rendered.duration_sec,
                num_raw_clips=planners_result.num_raw_clips,
                num_planned_clips=planners_result.num_planned_clips,
                dialogue_audio_included=command.audio_mode == "dialogue",
                stage_timings_sec=timings,
                wall_clock_sec=time.monotonic() - started,
                model_usage=model_usage,
                model_usage_artifact=str(layout.workflow_model_usage),
                music_analysis_result=str(layout.music_analysis_result),
                video_material_name=video_view.name,
                music_material_name=music_view.name,
                music_material_directory=str(music_binding.memory_root),
                artifact_manifest_version=ARTIFACT_MANIFEST_VERSION,
                artifacts=artifacts,
                bundle_directory=str(layout.root),
            )
            publish_result(layout.workflow_result, result)
            return result

    # Convenient spelling for adapters that treat complete generation as run.
    run = execute_workflow

    def _component_paths(
        self,
        output_dir: Path | None,
        component: str,
    ) -> _ComponentInvocationPaths:
        if output_dir is not None:
            root = output_dir.expanduser().resolve()
            data_root = self._effective_configuration.data_root.resolve()
            try:
                root.relative_to(data_root)
            except ValueError:
                pass
            else:
                raise ValueError(
                    "An explicit output directory must be outside the "
                    "Application Data Root"
                )
            root.mkdir(parents=True, exist_ok=True)
            return _ComponentInvocationPaths(
                workspace=root,
                log_path=root / "cutmaster.log",
            )
        layout = DirectArtifactLayout.allocate(
            self._effective_configuration.data_root,
            None,
        )
        return _ComponentInvocationPaths(
            workspace=layout.root.joinpath(*component.split("/")),
            log_path=layout.workflow_log,
        )

    def _resolve_or_ensure(
        self,
        material_type: MaterialType,
        *,
        source_path: Path | None,
        existing_name: str,
        candidate_name: str,
    ) -> MaterialView:
        if source_path is not None:
            return self._materials.ensure(
                source_path.resolve(),
                material_type,
                candidate_name or None,
            )
        view = self._materials.find_by_name(material_type, existing_name)
        if view is None:
            raise FileNotFoundError(
                f"No {material_type.value} Material named {existing_name!r}"
            )
        if not self._materials.verify(view.material_id):
            raise ValueError(
                f"Material {material_type.value}/{view.name!r} is inconsistent"
            )
        return view

    @staticmethod
    def _runtime_handle(
        binding: MaterialBinding,
        *,
        subtitle_path: Path | None = None,
    ) -> MaterialRuntimeHandle:
        material = binding.material
        managed_subtitle: Path | None = None
        if subtitle_path is not None:
            if subtitle_path.is_symlink():
                raise ValueError(
                    "A runtime subtitle handle must reference a managed "
                    "regular file"
                )
            managed_subtitle = subtitle_path.resolve(strict=True)
            if not managed_subtitle.is_file():
                raise ValueError(
                    "A runtime subtitle handle must reference a managed "
                    "regular file"
                )
            try:
                managed_subtitle.relative_to(binding.source_path.parent)
            except ValueError as exc:
                raise ValueError(
                    "External subtitle paths cannot enter a workflow stage"
                ) from exc
        return MaterialRuntimeHandle(
            material_id=material.material_id,
            material_type=material.material_type,
            material_name=material.name,
            expected_fingerprint=material.fingerprint,
            source_path=binding.source_path,
            memory_root=binding.memory_root,
            subtitle_path=managed_subtitle,
        )

    def _analyse_video(
        self,
        binding: MaterialBinding,
        workspace: Path,
        *,
        video_title: str,
        subtitle_path: Path | None,
        material_reused: bool,
    ) -> VideoAnalysisResult:
        if subtitle_path is not None:
            managed_subtitle = self._materials.ensure_subtitle(
                binding,
                subtitle_path,
            )
        else:
            managed_subtitle = self._materials.resolve_subtitle(binding)
        from cutmaster.workflow.analyser import Analyser

        required = {"llm", "vlm"}
        if managed_subtitle is None:
            required.add("asr")
        config = resolve_runtime_config(
            self._effective_configuration,
            required=frozenset(required),
        )
        result = Analyser(config).analyse(
            AnalyseVideoRequest(
                material=self._runtime_handle(
                    binding,
                    subtitle_path=managed_subtitle,
                ),
                options=VideoAnalysisOptions(video_title=video_title),
                workspace=AnalysisWorkspace(workspace.resolve()),
            )
        )
        result = replace(result, material_reused=material_reused)
        staged_result = result.write(workspace / "analysis_result.json")
        self._materials.publish_analysis_result(binding, staged_result)
        self._copy_memory_artifact(result.source_srt_path, workspace / "source.srt")
        self._copy_memory_artifact(
            result.processed_subtitle_path,
            workspace / "dialogue_merged.srt",
        )
        self._copy_memory_artifact(
            result.video.dialogues_path,
            workspace / "dialogues.json",
        )
        return result

    def _load_video_analysis(
        self,
        binding: MaterialBinding,
        workspace: Path,
        *,
        material_reused: bool,
    ) -> VideoAnalysisResult:
        """Load one already-published Video Memory without changing its spec."""

        canonical = binding.memory_root / "analysis_result.json"
        if binding.material.condition is not MaterialCondition.READY:
            raise FileNotFoundError(
                f"Video Material {binding.material.name!r} has no valid analysis"
            )
        result = replace(
            VideoAnalysisResult.read(canonical),
            material_reused=material_reused,
            analysis_reused=True,
            elapsed_sec=0.0,
            model_usage_summary={},
        )
        self._require_analysis_binding(result.video.material, binding)
        result.write(workspace / "analysis_result.json")
        self._copy_memory_artifact(result.source_srt_path, workspace / "source.srt")
        self._copy_memory_artifact(
            result.processed_subtitle_path,
            workspace / "dialogue_merged.srt",
        )
        self._copy_memory_artifact(
            result.video.dialogues_path,
            workspace / "dialogues.json",
        )
        return result

    def _load_music_analysis(
        self,
        binding: MaterialBinding,
        workspace: Path,
        *,
        material_reused: bool,
    ) -> MusicAnalysisResult:
        """Load one already-published reusable Music Memory."""

        canonical = binding.memory_root / "music_analysis_result.json"
        if binding.material.condition is not MaterialCondition.READY:
            raise FileNotFoundError(
                f"Music Material {binding.material.name!r} has no valid analysis"
            )
        result = replace(
            MusicAnalysisResult.read(canonical),
            material_reused=material_reused,
            analysis_reused=True,
            elapsed_sec=0.0,
        )
        self._require_analysis_binding(result.music.material, binding)
        result.write(workspace / "music_analysis_result.json")
        self._copy_memory_artifact(
            result.music.music_memory_path,
            workspace / "music_memory.json",
        )
        return result

    def _analyse_music(
        self,
        binding: MaterialBinding,
        workspace: Path,
        *,
        material_reused: bool,
    ) -> MusicAnalysisResult:
        from cutmaster.workflow.analyser import Analyser

        config = resolve_runtime_config(self._effective_configuration)
        result = Analyser(config).analyse_music(
            AnalyseMusicRequest(
                material=self._runtime_handle(binding),
                options=MusicAnalysisOptions(),
                workspace=AnalysisWorkspace(workspace.resolve()),
            )
        )
        result = replace(result, material_reused=material_reused)
        staged_result = result.write(workspace / "music_analysis_result.json")
        self._materials.publish_analysis_result(binding, staged_result)
        self._copy_memory_artifact(
            result.music.music_memory_path,
            workspace / "music_memory.json",
        )
        return result

    def _plan(
        self,
        analysis: VideoAnalysisResult,
        music_analysis: MusicAnalysisResult,
        workspace: Path,
        *,
        prompt: str,
        target_output_length_sec: float,
        target_shot_length_sec: float,
        prompt_type: str,
        video_title: str,
        max_clip_duration_sec: float | None,
        overwrite: bool,
        progress_reporter: ProgressReporter | None = None,
    ):
        from cutmaster.workflow.planners import Planners

        config = resolve_runtime_config(
            self._effective_configuration,
            required=frozenset({"llm", "vlm"}),
        )
        return Planners(config).plan(
            PlannersRequest(
                video=analysis.video,
                music=music_analysis.music,
                brief=PlannersBrief(prompt, target_output_length_sec),
                options=PlannersOptions(
                    target_shot_length_sec=target_shot_length_sec,
                    prompt_type=prompt_type,
                    video_title=(
                        video_title or analysis.video.material.material_name
                    ),
                    max_clip_duration_sec=max_clip_duration_sec,
                ),
                workspace=PlannersWorkspace(workspace.resolve()),
            ),
            overwrite=overwrite,
            progress_reporter=progress_reporter,
        )

    @staticmethod
    def _copy_memory_artifact(source: Path, destination: Path) -> None:
        """Project one immutable Material Memory file into a Direct Bundle."""

        if source.is_symlink():
            raise ValueError(
                f"Material Memory artifact must be a regular file: {source}"
            )
        resolved_source = source.resolve(strict=True)
        if not resolved_source.is_file():
            raise ValueError(
                f"Material Memory artifact must be a regular file: {source}"
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.resolve() == resolved_source:
            return
        shutil.copy2(resolved_source, destination)

    def _render(
        self,
        plan: RenderPlan,
        bindings: RenderRuntimeBindings,
        workspace: Path,
        *,
        audio_mode: str,
        overwrite: bool,
    ):
        from cutmaster.workflow.renderer import Renderer

        config = resolve_runtime_config(self._effective_configuration)
        return Renderer(config.renderer).render(
            RenderRequest(
                plan=plan,
                bindings=bindings,
                options=RenderOptions(audio_mode=audio_mode),
                output_target=RenderOutputTarget(workspace.resolve()),
            ),
            overwrite=overwrite,
        )

    def _resolve_video_for_plan(
        self,
        command: PlanCommand,
        stack: ExitStack,
    ) -> tuple[MaterialView, MaterialBinding, VideoAnalysisResult]:
        if (command.analysis_result_path is None) == (not command.video_material):
            raise ValueError(
                "Exactly one of analysis_result_path or video_material is required"
            )
        if command.analysis_result_path is not None:
            result = VideoAnalysisResult.read(command.analysis_result_path.resolve())
            view = self._materials.get(result.video.material.material_id)
        else:
            view = self._materials.find_by_name(
                MaterialType.VIDEO,
                command.video_material,
            )
            result = None
        if view is None:
            raise FileNotFoundError("Video Material was not found in the active catalog")
        binding = stack.enter_context(self._materials.lease(view.material_id))
        if result is None:
            result = VideoAnalysisResult.read(
                binding.memory_root / "analysis_result.json"
            )
        self._require_analysis_binding(result.video.material, binding)
        return view, binding, result

    def _resolve_music_for_plan(
        self,
        command: PlanCommand,
        stack: ExitStack,
        workspace: Path,
    ) -> tuple[MaterialView, MaterialBinding, MusicAnalysisResult]:
        selected = sum(
            value is not None and value != ""
            for value in (
                command.audio_path,
                command.music_analysis_result_path,
                command.music_material,
            )
        )
        if selected != 1:
            raise ValueError(
                "Exactly one music input (audio path, result, or Material) is required"
            )
        if command.audio_path is not None:
            view = self._materials.ensure(
                command.audio_path.resolve(),
                MaterialType.MUSIC,
                command.music_material_name or None,
            )
            binding = stack.enter_context(self._materials.lease(view.material_id))
            result = self._analyse_music(
                binding,
                workspace / "music_analysis",
                material_reused=view.reused,
            )
        elif command.music_analysis_result_path is not None:
            if command.music_material_name:
                raise ValueError(
                    "music_material_name is only valid with a raw audio path"
                )
            result = MusicAnalysisResult.read(
                command.music_analysis_result_path.resolve()
            )
            view = self._materials.get(result.music.material.material_id)
            if view is None:
                raise FileNotFoundError(
                    "Music Material was not found in the active catalog"
                )
            binding = stack.enter_context(self._materials.lease(view.material_id))
        else:
            if command.music_material_name:
                raise ValueError(
                    "music_material_name is only valid with a raw audio path"
                )
            view = self._materials.find_by_name(
                MaterialType.MUSIC,
                command.music_material,
            )
            if view is None:
                raise FileNotFoundError(
                    f"No music Material named {command.music_material!r}"
                )
            binding = stack.enter_context(self._materials.lease(view.material_id))
            result = MusicAnalysisResult.read(
                binding.memory_root / "music_analysis_result.json"
            )
        self._require_analysis_binding(result.music.material, binding)
        return view, binding, result

    @staticmethod
    def _require_analysis_binding(
        handle: MaterialRuntimeHandle,
        binding: MaterialBinding,
    ) -> None:
        material = binding.material
        if (
            handle.material_id != material.material_id
            or handle.material_type is not material.material_type
            or handle.material_name != material.name
            or handle.expected_fingerprint != material.fingerprint
            or handle.source_path.resolve() != binding.source_path
            or handle.memory_root.resolve() != binding.memory_root
        ):
            raise ValueError("Analysis result does not match its active Material")

    def _lease_many(
        self,
        material_ids: tuple[MaterialId, ...],
        stack: ExitStack,
    ) -> dict[MaterialId, MaterialBinding]:
        leased: dict[MaterialId, MaterialBinding] = {}
        for material_id in sorted(set(material_ids), key=str):
            leased[material_id] = stack.enter_context(
                self._materials.lease(material_id)
            )
        return leased

    def _lease_render_bindings(
        self,
        plan: RenderPlan,
        stack: ExitStack,
    ) -> RenderRuntimeBindings:
        leased = self._lease_many(
            (plan.video_material_id, plan.music_material_id),
            stack,
        )
        return RenderRuntimeBindings(
            video=self._runtime_handle(leased[plan.video_material_id]),
            music=self._runtime_handle(leased[plan.music_material_id]),
        )

    @staticmethod
    def _write_workflow_usage(
        layout: DirectArtifactLayout,
        analysis: VideoAnalysisResult,
        planners_result: Any,
        current_usage: dict[str, Any],
        cumulative_usage: dict[str, Any],
    ) -> dict[str, Any]:
        model_usage = {
            "schema_version": "1.0",
            "currency": "CNY",
            "price_unit": "yuan_per_million_tokens",
            "updated_at": datetime.now().astimezone().isoformat(),
            "current_run": current_usage,
            "cumulative": cumulative_usage,
            "stages": {
                "analyser": {
                    "current_run": analysis.model_usage_summary,
                    "cumulative": analysis.model_usage_cumulative_summary,
                    "artifact": (
                        str(analysis.model_usage_path)
                        if analysis.model_usage_path
                        else None
                    ),
                },
                "planners": {
                    "current_run": planners_result.model_usage_summary,
                    "cumulative": planners_result.model_usage_cumulative_summary,
                    "artifact": (
                        str(planners_result.model_usage_path)
                        if planners_result.model_usage_path
                        else None
                    ),
                },
            },
        }
        layout.workflow_model_usage.write_text(
            json.dumps(model_usage, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return model_usage


__all__ = ["DirectService"]
