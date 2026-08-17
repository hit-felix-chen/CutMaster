"""Application-owned execution boundary for reusable Material analysis."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from cutmaster.application.materials.service import MaterialsService
from cutmaster.application.ports.material_catalog import MaterialBinding
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.configuration.runtime import resolve_runtime_config
from cutmaster.configuration.schema import AppConfig
from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialCondition, MaterialType
from cutmaster.workflow.contracts.analysis import (
    AnalyseMusicRequest,
    AnalyseVideoRequest,
    AnalysisWorkspace,
    MusicAnalysisOptions,
    MusicAnalysisResult,
    VideoAnalysisOptions,
    VideoAnalysisResult,
)
from cutmaster.workflow.contracts.material import MaterialRuntimeHandle
from cutmaster.workflow.ports import CancellationToken, raise_if_cancelled

MaterialAnalysisResult = VideoAnalysisResult | MusicAnalysisResult


class MaterialAnalysisEngine(Protocol):
    """Workflow Analyser surface consumed by the Application Layer."""

    def analyse(
        self,
        request: AnalyseVideoRequest,
        *,
        cancellation_token: CancellationToken | None = None,
    ) -> VideoAnalysisResult: ...

    def analyse_music(
        self,
        request: AnalyseMusicRequest,
        *,
        cancellation_token: CancellationToken | None = None,
    ) -> MusicAnalysisResult: ...


class MaterialAnalysisEngineFactory(Protocol):
    def __call__(self, config: AppConfig) -> MaterialAnalysisEngine: ...


def _create_analyser(config: AppConfig) -> MaterialAnalysisEngine:
    # Opening CutMasterApplication must not load OpenCV or the model clients.
    # Import the Workflow implementation only for a real analysis invocation.
    from cutmaster.workflow.analyser import Analyser

    return Analyser(config)


@dataclass(frozen=True)
class ExecuteMaterialAnalysisCommand:
    """Analyse one registered Material in an Application-owned workspace.

    ``material_reused`` is invocation metadata from the import/ensure use case;
    it never participates in Material identity or cache validation.
    """

    material_id: MaterialId
    workspace: AnalysisWorkspace
    video_title: str = ""
    material_reused: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.material_id, MaterialId):
            raise TypeError("material_id must be a MaterialId")
        if not isinstance(self.workspace, AnalysisWorkspace):
            raise TypeError("workspace must be an AnalysisWorkspace")
        if not isinstance(self.video_title, str):
            raise TypeError("video_title must be a string")
        if not isinstance(self.material_reused, bool):
            raise TypeError("material_reused must be a boolean")


class ManagedMaterialAnalysisExecutor:
    """Lease a Material, run Analyser, and publish its canonical result.

    The command accepts only a Material identity and an invocation workspace.
    Source, Memory, fingerprint, and subtitle paths come exclusively from the
    verified ``MaterialsService`` lease and never from an inbound adapter.
    """

    def __init__(
        self,
        effective_configuration: EffectiveConfiguration,
        materials: MaterialsService,
    ) -> None:
        if not isinstance(effective_configuration, EffectiveConfiguration):
            raise TypeError("effective_configuration must be EffectiveConfiguration")
        if not isinstance(materials, MaterialsService):
            raise TypeError("materials must be MaterialsService")
        self._configuration = effective_configuration
        self._materials = materials

    def execute(
        self,
        command: ExecuteMaterialAnalysisCommand,
        *,
        cancellation_token: CancellationToken | None = None,
        analyser_factory: MaterialAnalysisEngineFactory = _create_analyser,
    ) -> MaterialAnalysisResult:
        if not isinstance(command, ExecuteMaterialAnalysisCommand):
            raise TypeError("command must be an ExecuteMaterialAnalysisCommand")
        workspace = _prepare_workspace(command.workspace)
        raise_if_cancelled(cancellation_token)
        with self._materials.lease(command.material_id) as binding:
            subtitle = (
                self._materials.resolve_subtitle(binding)
                if binding.material.material_type is MaterialType.VIDEO
                else None
            )
            handle = _runtime_handle(binding, subtitle_path=subtitle)
            if binding.material.condition is MaterialCondition.READY:
                return _load_published_result(handle, workspace)
            if binding.material.material_type is MaterialType.VIDEO:
                result = self._analyse_video(
                    command,
                    handle,
                    workspace,
                    cancellation_token=cancellation_token,
                    analyser_factory=analyser_factory,
                )
                staged = result.write(workspace.root / "analysis_result.json")
            else:
                result = self._analyse_music(
                    command,
                    handle,
                    workspace,
                    cancellation_token=cancellation_token,
                    analyser_factory=analyser_factory,
                )
                staged = result.write(workspace.root / "music_analysis_result.json")
            # A stopped Attempt may leave reusable checkpoints in Material
            # Memory, but it must never make the Material READY.
            raise_if_cancelled(cancellation_token)
            self._materials.publish_analysis_result(binding, staged)
            return result

    def _analyse_video(
        self,
        command: ExecuteMaterialAnalysisCommand,
        handle: MaterialRuntimeHandle,
        workspace: AnalysisWorkspace,
        *,
        cancellation_token: CancellationToken | None,
        analyser_factory: MaterialAnalysisEngineFactory,
    ) -> VideoAnalysisResult:
        required = {"llm", "vlm"}
        if handle.subtitle_path is None:
            required.add("asr")
        config = resolve_runtime_config(
            self._configuration,
            required=frozenset(required),
        )
        result = analyser_factory(config).analyse(
            AnalyseVideoRequest(
                material=handle,
                options=VideoAnalysisOptions(video_title=command.video_title),
                workspace=workspace,
            ),
            cancellation_token=cancellation_token,
        )
        return replace(result, material_reused=command.material_reused)

    def _analyse_music(
        self,
        command: ExecuteMaterialAnalysisCommand,
        handle: MaterialRuntimeHandle,
        workspace: AnalysisWorkspace,
        *,
        cancellation_token: CancellationToken | None,
        analyser_factory: MaterialAnalysisEngineFactory,
    ) -> MusicAnalysisResult:
        config = resolve_runtime_config(self._configuration)
        result = analyser_factory(config).analyse_music(
            AnalyseMusicRequest(
                material=handle,
                options=MusicAnalysisOptions(),
                workspace=workspace,
            ),
            cancellation_token=cancellation_token,
        )
        return replace(result, material_reused=command.material_reused)


def _prepare_workspace(workspace: AnalysisWorkspace) -> AnalysisWorkspace:
    candidate = workspace.root
    if candidate.is_symlink():
        raise ValueError(f"Analysis workspace is not a regular directory: {candidate}")
    root = candidate.resolve()
    if root.exists() and not root.is_dir():
        raise ValueError(f"Analysis workspace is not a regular directory: {root}")
    root.mkdir(parents=True, exist_ok=True)
    return AnalysisWorkspace(root)


def _runtime_handle(
    binding: MaterialBinding,
    *,
    subtitle_path: Path | None = None,
) -> MaterialRuntimeHandle:
    source = _require_regular_path(binding.source_path, "Material source")
    memory_root = binding.memory_root
    if memory_root.is_symlink():
        raise ValueError("Material Memory must be a managed regular directory")
    memory_root = memory_root.resolve(strict=True)
    if not memory_root.is_dir():
        raise ValueError("Material Memory must be a managed regular directory")

    managed_subtitle: Path | None = None
    if subtitle_path is not None:
        managed_subtitle = _require_regular_path(subtitle_path, "Material subtitle")
        try:
            managed_subtitle.relative_to(source.parent)
        except ValueError as exc:
            raise ValueError(
                "External subtitle paths cannot enter a workflow stage"
            ) from exc

    material = binding.material
    return MaterialRuntimeHandle(
        material_id=material.material_id,
        material_type=material.material_type,
        material_name=material.name,
        expected_fingerprint=material.fingerprint,
        source_path=source,
        memory_root=memory_root,
        subtitle_path=managed_subtitle,
    )


def _require_regular_path(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise ValueError(f"{label} must be a managed regular file")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"{label} must be a managed regular file") from exc
    if not resolved.is_file():
        raise ValueError(f"{label} must be a managed regular file")
    return resolved


def _load_published_result(
    handle: MaterialRuntimeHandle,
    workspace: AnalysisWorkspace,
) -> MaterialAnalysisResult:
    if handle.material_type is MaterialType.VIDEO:
        canonical = handle.memory_root / "analysis_result.json"
        result = replace(
            VideoAnalysisResult.read(canonical, handle),
            material_reused=True,
            analysis_reused=True,
            elapsed_sec=0.0,
            model_usage_summary={},
        )
        result.write(workspace.root / "analysis_result.json")
        return result
    canonical = handle.memory_root / "music_analysis_result.json"
    result = replace(
        MusicAnalysisResult.read(canonical, handle),
        material_reused=True,
        analysis_reused=True,
        elapsed_sec=0.0,
    )
    result.write(workspace.root / "music_analysis_result.json")
    return result


__all__ = [
    "ExecuteMaterialAnalysisCommand",
    "ManagedMaterialAnalysisExecutor",
    "MaterialAnalysisEngine",
    "MaterialAnalysisEngineFactory",
    "MaterialAnalysisResult",
]
