"""Application-owned execution boundary for one immutable ASTER Run snapshot."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from cutmaster.application.materials.service import MaterialsService
from cutmaster.application.ports.material_catalog import MaterialBinding
from cutmaster.application.runs.views import RunView
from cutmaster.configuration.effective import (
    EffectiveConfiguration,
    SecretReferences,
)
from cutmaster.configuration.runtime import resolve_runtime_config
from cutmaster.configuration.schema import AppConfig
from cutmaster.domain.ids import MaterialId
from cutmaster.domain.materials import MaterialCondition, MaterialType
from cutmaster.workflow.contracts.analysis import (
    MusicAnalysisResult,
    VideoAnalysisResult,
)
from cutmaster.workflow.contracts.material import MaterialRuntimeHandle
from cutmaster.workflow.contracts.planners import (
    PlannersBrief,
    PlannersOptions,
    PlannersRequest,
    PlannersResult,
    PlannersWorkspace,
)
from cutmaster.workflow.ports import (
    CancellationToken,
    ProgressReporter,
    raise_if_cancelled,
)

if TYPE_CHECKING:
    from cutmaster.workflow.contracts.checkpoints import PlannersCheckpointStore


class PlanningEngine(Protocol):
    """Transport-neutral ASTER planning engine used by the executor."""

    def plan(
        self,
        request: PlannersRequest,
        *,
        overwrite: bool = False,
        progress_reporter: ProgressReporter | None = None,
        cancellation_token: CancellationToken | None = None,
        checkpoint_store: PlannersCheckpointStore | None = None,
    ) -> PlannersResult: ...


PlanningEngineFactory = Callable[[AppConfig], PlanningEngine]


@dataclass(frozen=True, kw_only=True)
class ExecuteRunPlanningCommand:
    """Execute ASTER from a Run snapshot and Application-owned runtime inputs.

    Material identity comes exclusively from ``run``. Paths below are ephemeral
    invocation capabilities: they are neither persisted nor accepted as
    substitutes for a Material ID.
    """

    run: RunView
    workspace: Path
    overwrite: bool = False
    progress_reporter: ProgressReporter | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    cancellation_token: CancellationToken | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    checkpoint_store: PlannersCheckpointStore | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.run, RunView):
            raise TypeError("run must be an immutable RunView snapshot")
        if not isinstance(self.workspace, Path):
            raise TypeError("workspace must be a pathlib.Path")
        if not self.workspace.is_absolute():
            raise ValueError("workspace must be an absolute Application-owned path")
        if not isinstance(self.overwrite, bool):
            raise TypeError("overwrite must be a boolean")


@dataclass(frozen=True)
class RunPlanningArtifacts:
    """Files produced by exactly one ASTER planning invocation."""

    render_plan: Path
    candidate_pool: Path
    edit_plan: Path
    dialogue_anchors: Path
    raw_script: Path
    music_profile: Path
    selection_diagnostics: Path
    model_usage_summary: Mapping[str, Any]

    @classmethod
    def from_result(
        cls,
        result: PlannersResult,
        workspace: Path,
    ) -> "RunPlanningArtifacts":
        root = workspace.resolve()
        paths = {
            "render_plan": result.render_plan_path,
            "candidate_pool": result.candidate_pool_path,
            "edit_plan": result.edit_plan_path,
            "dialogue_anchors": result.dialogue_anchors_path,
            "raw_script": result.raw_script_path,
            "music_profile": result.music_profile_path,
            "selection_diagnostics": result.selection_diagnostics_path,
        }
        resolved: dict[str, Path] = {}
        for name, path in paths.items():
            if not isinstance(path, Path):
                raise TypeError(f"{name} artifact must be a pathlib.Path")
            if path.is_symlink():
                raise ValueError(f"ASTER produced a symlinked {name} artifact")
            candidate = path.resolve(strict=True)
            if not candidate.is_file():
                raise ValueError(f"ASTER did not produce a regular {name} artifact")
            try:
                candidate.relative_to(root)
            except ValueError as exc:
                raise ValueError(
                    f"ASTER {name} artifact escaped the planning workspace"
                ) from exc
            resolved[name] = candidate
        return cls(
            render_plan=resolved["render_plan"],
            candidate_pool=resolved["candidate_pool"],
            edit_plan=resolved["edit_plan"],
            dialogue_anchors=resolved["dialogue_anchors"],
            raw_script=resolved["raw_script"],
            music_profile=resolved["music_profile"],
            selection_diagnostics=resolved["selection_diagnostics"],
            model_usage_summary=MappingProxyType(dict(result.model_usage_summary)),
        )


class RunPlanningExecutor:
    """Lease snapshotted Materials and execute ASTER without an Adapter dependency."""

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
        command: ExecuteRunPlanningCommand,
        *,
        planner_factory: PlanningEngineFactory | None = None,
    ) -> RunPlanningArtifacts:
        if not isinstance(command, ExecuteRunPlanningCommand):
            raise TypeError("command must be an ExecuteRunPlanningCommand")
        run = command.run
        if len(run.video_material_ids) != 1 or len(run.music_material_ids) != 1:
            raise ValueError(
                "ASTER Run snapshot requires exactly one video and one music"
            )
        _validate_workspace(command.workspace)
        raise_if_cancelled(command.cancellation_token)

        with ExitStack() as stack:
            video_binding = stack.enter_context(
                self._materials.consume_lease(run.video_material_ids[0])
            )
            music_binding = stack.enter_context(
                self._materials.consume_lease(run.music_material_ids[0])
            )
            video = _load_video_analysis(
                video_binding,
                run.video_material_ids[0],
            )
            music = _load_music_analysis(
                music_binding,
                run.music_material_ids[0],
            )
            raise_if_cancelled(command.cancellation_token)

            snapshot = _snapshot_configuration(self._configuration, run.configuration)
            runtime = resolve_runtime_config(
                snapshot,
                required=frozenset({"llm", "vlm"}),
            )
            options = _planners_options(run.planning_options)
            request = PlannersRequest(
                video=video.video,
                music=music.music,
                brief=PlannersBrief(
                    run.creative_brief.editing_intent,
                    run.creative_brief.target_duration_sec,
                ),
                options=PlannersOptions(
                    target_shot_length_sec=options["target_shot_length_sec"],
                    prompt_type=options["prompt_type"],
                    video_title=(
                        options["video_title"]
                        or video.video.material.material_name
                    ),
                    max_clip_duration_sec=options["max_clip_duration_sec"],
                ),
                workspace=PlannersWorkspace(command.workspace.resolve()),
            )
            if planner_factory is None:
                # Import only on an executing path so Application inspection does
                # not eagerly import model-provider clients.
                from cutmaster.workflow.planners import Planners

                planner_factory = Planners
            result = planner_factory(runtime).plan(
                request,
                overwrite=command.overwrite,
                progress_reporter=command.progress_reporter,
                cancellation_token=command.cancellation_token,
                checkpoint_store=command.checkpoint_store,
            )
            raise_if_cancelled(command.cancellation_token)
            return RunPlanningArtifacts.from_result(result, command.workspace)


def _validate_workspace(workspace: Path) -> None:
    if workspace.is_symlink():
        raise ValueError("Planning workspace must not be a symlink")
    if workspace.exists() and not workspace.is_dir():
        raise ValueError("Planning workspace must be a directory")


def _load_video_analysis(
    binding: MaterialBinding,
    expected_id: MaterialId,
) -> VideoAnalysisResult:
    _require_binding(binding, expected_id, MaterialType.VIDEO)
    handle = _runtime_handle(binding)
    return VideoAnalysisResult.read(
        _regular_file(binding.memory_root / "analysis_result.json", "Video analysis"),
        handle,
    )


def _load_music_analysis(
    binding: MaterialBinding,
    expected_id: MaterialId,
) -> MusicAnalysisResult:
    _require_binding(binding, expected_id, MaterialType.MUSIC)
    handle = _runtime_handle(binding)
    return MusicAnalysisResult.read(
        _regular_file(
            binding.memory_root / "music_analysis_result.json",
            "Music analysis",
        ),
        handle,
    )


def _require_binding(
    binding: MaterialBinding,
    expected_id: MaterialId,
    expected_type: MaterialType,
) -> None:
    if not isinstance(binding, MaterialBinding):
        raise TypeError("Material lease must yield a MaterialBinding")
    material = binding.material
    if material.material_id != expected_id:
        raise ValueError("Material lease does not belong to the ASTER Run snapshot")
    if material.material_type is not expected_type:
        raise ValueError(
            f"ASTER Run {expected_type.value} Material has the wrong type"
        )
    if material.condition is not MaterialCondition.READY:
        raise ValueError(f"ASTER Run {expected_type.value} Material is not ready")


def _runtime_handle(binding: MaterialBinding) -> MaterialRuntimeHandle:
    material = binding.material
    source_path = _regular_file(binding.source_path, "Material source")
    if binding.memory_root.is_symlink():
        raise ValueError("Material Memory must be a managed regular directory")
    memory_root = binding.memory_root.resolve(strict=True)
    if not memory_root.is_dir():
        raise ValueError("Material Memory must be a managed regular directory")
    return MaterialRuntimeHandle(
        material_id=material.material_id,
        material_type=material.material_type,
        material_name=material.name,
        expected_fingerprint=material.fingerprint,
        source_path=source_path,
        memory_root=memory_root,
    )


def _regular_file(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise ValueError(f"{label} must be a managed regular file")
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise ValueError(f"{label} must be a managed regular file") from exc
    if not resolved.is_file():
        raise ValueError(f"{label} must be a managed regular file")
    return resolved


def _snapshot_configuration(
    current: EffectiveConfiguration,
    values: Mapping[str, Any],
) -> EffectiveConfiguration:
    return EffectiveConfiguration(
        sources=current.sources,
        data_root=current.data_root,
        secret_references=_snapshot_secret_references(values),
        _values=values,
    )


def _snapshot_secret_references(values: Mapping[str, Any]) -> SecretReferences:
    def reference(*sections: str) -> str | None:
        value: Any = values
        for section in sections:
            if not isinstance(value, Mapping):
                return None
            value = value.get(section)
        if not isinstance(value, Mapping):
            return None
        raw = value.get("api_key_env")
        return None if raw is None else str(raw)

    return SecretReferences(
        llm_api_key_env=reference("llm"),
        vlm_api_key_env=reference("vlm"),
        asr_api_key_env=reference("analyser", "asr"),
    )


def _planners_options(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "target_shot_length_sec": float(raw.get("target_shot_length_sec", 4.0)),
        "prompt_type": str(raw.get("prompt_type") or "event"),
        "video_title": str(raw.get("video_title") or ""),
        "max_clip_duration_sec": (
            None
            if raw.get("max_clip_duration_sec") is None
            else float(raw["max_clip_duration_sec"])
        ),
    }


__all__ = [
    "ExecuteRunPlanningCommand",
    "PlanningEngine",
    "PlanningEngineFactory",
    "RunPlanningArtifacts",
    "RunPlanningExecutor",
]
