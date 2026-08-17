"""Application-owned execution boundary for one managed Render Variant Attempt."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol
from uuid import uuid4

from cutmaster.application.errors import ApplicationError, RenderFpsMismatchError
from cutmaster.application.materials.service import MaterialsService
from cutmaster.application.ports.material_catalog import MaterialBinding
from cutmaster.application.renders.commands import CompleteRenderVariantCommand
from cutmaster.application.renders.views import CompletedRenderView
from cutmaster.configuration.effective import EffectiveConfiguration
from cutmaster.configuration.schema import RendererConfig
from cutmaster.domain.ids import AttemptId, RenderVariantId, RunId
from cutmaster.domain.renders import RenderVariantStatus
from cutmaster.workflow.contracts.material import (
    MaterialRuntimeHandle,
    RenderRuntimeBindings,
)
from cutmaster.workflow.contracts.rendering import (
    RenderOptions,
    RenderOutputTarget,
    RenderRequest,
    RenderResult,
)
from cutmaster.workflow.renderer import Renderer
from cutmaster.workflow.shared.video_cover import write_video_cover

if TYPE_CHECKING:
    from cutmaster.application.renders.service import RendersService


class RenderExecutionInterrupted(ApplicationError):
    """The caller requested stop at a safe managed Render checkpoint."""

    code = "render_execution_interrupted"


class RenderEngine(Protocol):
    def render(
        self,
        request: RenderRequest,
        *,
        overwrite: bool = False,
    ) -> RenderResult: ...


RenderEngineFactory = Callable[[RendererConfig], RenderEngine]
StopCheck = Callable[[], bool]
ProgressUpdate = Callable[[Mapping[str, object]], None]
CoverGenerator = Callable[[Path, Path], Path]


@dataclass(frozen=True)
class ExecuteManagedRenderCommand:
    render_variant_id: RenderVariantId
    attempt_id: AttemptId


class ManagedRenderExecutor:
    """Lease inputs, run Renderer in owned staging, and atomically commit master."""

    def __init__(
        self,
        effective_configuration: EffectiveConfiguration,
        materials: MaterialsService,
        renders: RendersService,
    ) -> None:
        self._configuration = effective_configuration
        self._materials = materials
        self._renders = renders

    def execute(
        self,
        command: ExecuteManagedRenderCommand,
        *,
        should_stop: StopCheck,
        report_progress: ProgressUpdate,
        renderer_factory: RenderEngineFactory = Renderer,
        cover_generator: CoverGenerator | None = None,
    ) -> CompletedRenderView:
        if not isinstance(command.render_variant_id, RenderVariantId):
            raise TypeError("render_variant_id must be a RenderVariantId")
        if not isinstance(command.attempt_id, AttemptId):
            raise TypeError("attempt_id must be an AttemptId")
        from cutmaster.application.runs.review import load_render_plan

        variant = self._renders.get(command.render_variant_id)
        if variant.status is not RenderVariantStatus.RENDERING:
            raise ValueError("Managed Render execution requires a Rendering variant")
        specification = self._renders.specification(command.render_variant_id)
        edit = self._renders._store.get_frozen_edit(variant.edit_id)
        run = self._renders._store.get_run(RunId.parse(str(edit["run_id"])))
        plan, _ = load_render_plan(
            self._configuration.data_root,
            str(edit["plan_relative_path"]),
        )
        if tuple(run["video_material_ids"]) != (str(plan.video_material_id),):
            raise ValueError("RenderPlan video Material does not belong to its Run")
        if tuple(run["music_material_ids"]) != (str(plan.music_material_id),):
            raise ValueError("RenderPlan music Material does not belong to its Run")
        if plan.fps != specification.renderer.fps:
            raise RenderFpsMismatchError(
                f"RenderPlan fps {plan.fps} does not match snapshotted renderer fps "
                f"{specification.renderer.fps}"
            )
        if should_stop():
            raise RenderExecutionInterrupted("Render stopped before input preparation")
        report_progress(_progress("preparing", "running"))
        owner = _owned_render_directory(
            self._configuration.data_root,
            str(run["project_id"]),
            command.render_variant_id,
        )
        staging = Path(
            tempfile.mkdtemp(
                prefix=f".attempt-{command.attempt_id}-",
                dir=owner,
            )
        )
        master = owner / "master.mp4"
        cover = owner / "cover.jpg"
        backup = staging / ".previous-master.mp4"
        backup_cover = staging / ".previous-cover.jpg"
        committed = False
        master_published = False
        cover_published = False
        try:
            with ExitStack() as stack:
                video_binding = stack.enter_context(
                    self._materials.consume_lease(plan.video_material_id)
                )
                music_binding = stack.enter_context(
                    self._materials.consume_lease(plan.music_material_id)
                )
                video = _runtime_handle(
                    video_binding,
                    subtitle_path=self._materials.resolve_subtitle(video_binding),
                )
                music = _runtime_handle(music_binding)
                if should_stop():
                    raise RenderExecutionInterrupted(
                        "Render stopped before the media operation"
                    )
                report_progress(_progress("rendering", "running"))
                result = renderer_factory(
                    specification.renderer.to_config()
                ).render(
                    RenderRequest(
                        plan=plan,
                        bindings=RenderRuntimeBindings(video=video, music=music),
                        options=RenderOptions(specification.audio_mode),
                        output_target=RenderOutputTarget(
                            root=staging,
                            output_filename="candidate.mp4",
                        ),
                    ),
                    overwrite=False,
                )
            output = result.output_video
            if output.is_symlink() or not output.is_file():
                raise ValueError("Renderer did not produce a regular master candidate")
            if output.parent.resolve() != staging.resolve():
                raise ValueError("Renderer output escaped the managed staging directory")
            candidate_cover = staging / "cover.jpg"
            (cover_generator or _write_render_cover)(output, candidate_cover)
            if should_stop():
                raise RenderExecutionInterrupted(
                    "Render stopped after the media operation and before publication"
                )
            report_progress(_progress("publishing", "running"))
            _fsync_file(output)
            _fsync_file(candidate_cover)
            if master.exists() or master.is_symlink():
                os.replace(master, backup)
            if cover.exists() or cover.is_symlink():
                os.replace(cover, backup_cover)
            os.replace(output, master)
            master_published = True
            os.replace(candidate_cover, cover)
            cover_published = True
            _fsync_directory(owner)
            completed = self._renders.complete(
                CompleteRenderVariantCommand(
                    command_id=str(uuid4()),
                    render_variant_id=command.render_variant_id,
                    attempt_id=command.attempt_id,
                    master_relative_path=(
                        f"projects/{run['project_id']}/renders/"
                        f"{command.render_variant_id}/master.mp4"
                    ),
                    frame_count=result.frames,
                    duration_sec=result.duration_sec,
                )
            )
            committed = True
            return completed
        finally:
            if not committed:
                rolled_back = False
                if master_published and (master.exists() or master.is_symlink()):
                    master.unlink()
                    rolled_back = True
                if cover_published and (cover.exists() or cover.is_symlink()):
                    cover.unlink()
                    rolled_back = True
                if backup.exists() or backup.is_symlink():
                    os.replace(backup, master)
                    rolled_back = True
                if backup_cover.exists() or backup_cover.is_symlink():
                    os.replace(backup_cover, cover)
                    rolled_back = True
                if rolled_back:
                    _fsync_directory(owner)
            shutil.rmtree(staging, ignore_errors=False)


def _runtime_handle(
    binding: MaterialBinding,
    *,
    subtitle_path: Path | None = None,
) -> MaterialRuntimeHandle:
    material = binding.material
    return MaterialRuntimeHandle(
        material_id=material.material_id,
        material_type=material.material_type,
        material_name=material.name,
        expected_fingerprint=material.fingerprint,
        source_path=binding.source_path,
        memory_root=binding.memory_root,
        subtitle_path=subtitle_path,
    )


def _write_render_cover(source_path: Path, destination_path: Path) -> Path:
    return write_video_cover(source_path, destination_path, (0.0,))


def _owned_render_directory(
    data_root: Path,
    project_id: str,
    render_id: RenderVariantId,
) -> Path:
    root = data_root.resolve()
    current = root
    for component in ("projects", project_id, "renders", str(render_id)):
        current = current / component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            current.mkdir()
            metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise ValueError("Managed Render owner directory is unsafe")
    if current.resolve() != current:
        raise ValueError("Managed Render owner directory escaped the Data Root")
    return current


def _fsync_file(path: Path) -> None:
    with path.open("rb") as stream:
        os.fsync(stream.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _progress(phase: str, state: str) -> Mapping[str, object]:
    return {
        "schema_version": "1.0",
        "phase": phase,
        "state": state,
        "stop_behavior": (
            "after_current_media_operation"
            if phase == "rendering"
            else "at_checkpoint"
        ),
    }


__all__ = [
    "ExecuteManagedRenderCommand",
    "ManagedRenderExecutor",
    "RenderEngine",
    "RenderEngineFactory",
    "RenderExecutionInterrupted",
    "CoverGenerator",
]
